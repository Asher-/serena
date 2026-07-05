"""
Cursor-based code navigation for Serena.

Provides a stateful cursor that can be positioned on a symbol and moved along LSP graph edges
(contains, references, calls, type hierarchy) for incremental exploration.
"""

import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from serena.project import Project
from serena.symbol import LanguageServerSymbol, LanguageServerSymbolLocation, LanguageServerSymbolRetriever
from serena.util.line_numbers import format_line_range, to_display_line
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_utils import PathUtils
from solidlsp.lsp_protocol_handler.lsp_types import (
    CallHierarchyItem,
    SymbolKind,
    TypeHierarchyItem,
)
from solidlsp.structural.backends.plaintext import PlaintextView
from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.kinds import KindName
from solidlsp.structural.registry import StructuralBackendRegistry, default_plaintext_floor, default_structural_backend_registry

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
# agent must opt in to the edges it wants -- either at start time via the
# ``edge_types`` parameter on ``cursor_start`` / ``cursor_find``, or after
# the fact via ``cursor_configure``.
DEFAULT_EDGE_TYPES: frozenset[EdgeType] = frozenset()


class ReadRung(Enum):
    """Rungs of the single read-resolution ladder (spec-v2 §5.1).

    Ordered strongest->floor. Every readable file resolves to exactly one rung
    and no rung raises a terminal error: ``LSP`` is the language-server surface
    (symbols, references, call/type hierarchy); ``STRUCTURAL`` is a registered
    structural backend (json/yaml/toml/... and, later, tree-sitter);
    ``PLAINTEXT`` is the universal floor for any file no richer rung claims.
    :meth:`CursorManager.resolve_read_rung` is the one place ``cursor_overview``
    and ``cursor_grep`` agree with ``cursor_start`` about how a file is read.
    """

    LSP = "lsp"
    STRUCTURAL = "structural"
    PLAINTEXT = "plaintext"

# operations that require the cursor to be positioned on a container node
_CONTAINER_POSITIONED_OPERATIONS: frozenset[str] = frozenset({"insert_start", "insert_end"})
# operations that require the cursor to be positioned on a container's member
_MEMBER_ANCHORED_OPERATIONS: frozenset[str] = frozenset({"insert_before", "insert_after", "replace", "remove"})
# kinds that guarantee the cursor addresses a non-container leaf. Only the
# kinds on this list can be pre-flagged without inspecting the parsed tree:
#
# * ``container_member`` -- Python's kind for a dict/list member whose value is
#   *not* itself a dict or list (the walk re-yields container-valued members
#   with kind ``container``, overwriting the cache entry; so a cached
#   ``container_member`` is definitionally a scalar-valued member).
# * ``string`` / ``number`` / ``boolean`` / ``null`` -- JSON array-item scalar
#   kinds emitted by ``_value_kind``.
# * ``scalar`` -- the TOML / YAML catch-all for array-item scalars.
#
# JSON ``member``, TOML ``pair``, YAML ``pair`` are deliberately absent: the
# walk tags every mapping member with those kinds regardless of whether the
# value is scalar or compound, so they are not a reliable pre-flag signal.
_STRUCTURAL_SCALAR_KINDS: frozenset[str] = frozenset({"container_member", "string", "number", "boolean", "null", "scalar"})

# Maximum siblings/contains entries shown inline before truncation.
_MAX_INLINE_LIST = 12
# Last-N hops shown in the trail block (excludes the current position marker).
_TRAIL_TAIL_LENGTH = 5
# Character cap for the gist line so the projection stays compact.
_GIST_MAX_CHARS = 140


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
            return f"{self.relative_path}:{to_display_line(self.line)}"
        elif self.relative_path:
            return self.relative_path
        return "?"

    def format_compact(self) -> str:
        """Render as ``name :Kind@file:line:`` -- a single, parseable handle.

        ``Kind`` is omitted when empty (e.g. the REFERENCES edge yields
        unnamed-kind targets) so the result still reads cleanly. ``detail``
        (an optional one-liner attached to call-hierarchy items) is
        appended after the location with an em-dash separator so a reader
        can quote it without paraphrasing.
        """
        if self.kind:
            head = f"{self.name} :{self.kind}@{self.location_str}:"
        else:
            head = f"{self.name} @{self.location_str}:"
        if self.detail:
            return f"{head}  -- {self.detail}"
        return head


@dataclass(frozen=True)
class CursorTrailEntry:
    """A prior position recorded by ``CursorState.record_move``.

    Captures the symbol's name and kind at trail-write time alongside its
    location so the rendered trail survives subsequent edits without an
    LSP round-trip. Read by ``CursorManager._render_trail`` to project
    the trail as a chain of ``name :Kind@file:line:`` handles.

    :ivar relative_path: project-relative path of the symbol identifier.
    :ivar line: 0-indexed line of the symbol's selection range start.
    :ivar column: 0-indexed column of the symbol's selection range start.
    :ivar name: the symbol's plain name as captured at trail-write time.
    :ivar kind: the symbol's LSP kind name (e.g. ``"Method"``) at
        trail-write time. Empty string when unavailable.
    """

    relative_path: str | None
    line: int | None
    column: int | None
    name: str
    kind: str


@dataclass

class CursorState:
    """The state of a single navigation cursor.

    Carries the cursor's current position, its trail of prior positions,
    the active LSP-edge set used when neighbors are resolved, a small
    bundle of *projection toggles* controlling which optional sections of
    :meth:`CursorManager.format_cursor_view` render, and the last
    *reasoning* string (the agent's stated semantic goal for being at
    this position). The toggles default to ``False`` so a fresh cursor
    projects just its anchor (plus any edge blocks the active edge set
    produces): the agent opts into additional layers via
    ``cursor_configure`` when context warrants. ``last_reasoning`` is
    updated by every navigational tool call (``cursor_start`` /
    ``cursor_find`` / ``cursor_move`` / ``cursor_narrate``) so the
    projection always carries the most-recently-articulated intent
    above the anchor.

    :ivar cursor_id: stable handle the manager uses to look up the cursor.
    :ivar current_symbol: the LSP symbol the cursor currently addresses.
    :ivar current_location: a snapshot of ``current_symbol``'s location
        captured at cursor-creation / move time.
    :ivar trail: prior positions recorded by :meth:`record_move`.
    :ivar active_edge_types: edges resolved when the projection renders
        neighbors. Empty (the default) means no edges are queried.
    :ivar include_body: when ``True``, the projection appends a
        ``--- body ---`` block with the symbol's source.
    :ivar include_chain: when ``True``, the projection includes the
        ascending containment chain ending at the file path.
    :ivar include_trail: when ``True``, the projection includes the
        last-N prior hops with a ``<- here`` marker on the current.
    :ivar include_siblings: when ``True``, the projection includes the
        inline list of peer names under the same parent.
    :ivar include_gist: when ``True``, the projection includes a
        one-line extract of the symbol body.
    :ivar last_reasoning: the agent's most-recent stated semantic goal.
        Rendered as ``why: <text>`` above the anchor whenever set.
        ``None`` (the default) suppresses the line entirely.
    """

    cursor_id: str
    current_symbol: LanguageServerSymbol
    current_location: LanguageServerSymbolLocation
    trail: list[CursorTrailEntry] = field(default_factory=list)
    active_edge_types: frozenset[EdgeType] = DEFAULT_EDGE_TYPES
    include_body: bool = False
    include_chain: bool = False
    include_trail: bool = False
    include_siblings: bool = False
    include_gist: bool = False
    last_reasoning: str | None = None

    def record_move(self, new_symbol: LanguageServerSymbol, new_location: LanguageServerSymbolLocation) -> None:
        """Record moving the cursor to a new symbol.

        Stores the symbol-side metadata (name + kind) of the position
        being left so the trail rendering can show a ``name :Kind`` handle
        for each prior hop without re-querying the language server.
        """
        # snapshot the position we are leaving as a self-describing trail entry
        self.trail.append(
            CursorTrailEntry(
                relative_path=self.current_location.relative_path,
                line=self.current_location.line,
                column=self.current_location.column,
                name=self.current_symbol.name,
                kind=self.current_symbol.symbol_kind_name,
            )
        )
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
    :ivar include_chain: when ``True``, the projection includes the
        ascending name-path chain ending at the file path. Default ``False``.
    :ivar include_trail: when ``True``, the projection includes the
        last-N prior name paths with a ``<- here`` marker on the current.
        Default ``False``.
    :ivar include_siblings: when ``True``, the projection includes the
        inline list of peer member name paths under the same parent.
        Default ``False``.
    :ivar include_gist: when ``True``, the projection includes a
        one-line extract of the serialized node text. Default ``False``.
    :ivar last_reasoning: the agent's most-recent stated semantic goal.
        Rendered as ``why: <text>`` above the anchor whenever set.
        ``None`` (the default) suppresses the line entirely.
    """

    cursor_id: str
    relative_path: str
    name_path: str
    kind: KindName
    trail: list[str] = field(default_factory=list)
    include_body: bool = False
    include_chain: bool = False
    include_trail: bool = False
    include_siblings: bool = False
    include_gist: bool = False
    last_reasoning: str | None = None


@dataclass
class PlaintextCursorState:
    """The state of a cursor positioned on a whole file at the plaintext floor rung.

    Used when neither the language server nor a structural backend claims the file
    (spec-v2 §5.1 rung3): the cursor addresses the file as a whole and its view is a
    :class:`~solidlsp.structural.backends.plaintext.PlaintextView` -- a line/size/
    encoding descriptor plus, when ``include_body`` is set, the byte-exact numbered
    body. The floor never raises, so ``cursor_start`` / ``cursor_look`` always land
    here rather than dead-ending. Editing a plaintext file is line-addressed
    (``cursor_replace_range``), so this cursor carries no structural edit surface.

    :ivar cursor_id: the cursor's handle.
    :ivar relative_path: POSIX-style project-relative path of the file.
    :ivar trail: unused at the plaintext rung; always empty -- a whole-file cursor
        does not navigate, so nothing is ever appended. Present for state uniformity.
    :ivar include_body: when ``True`` (the default -- the floor exists to show
        content), the projection appends the file's byte-exact numbered body.
        Mirrors the ``include_*`` toggles on the other cursor states so
        ``cursor_configure`` stays uniform across cursor kinds.
    :ivar include_chain: unused at the plaintext rung; present for toggle uniformity.
    :ivar include_trail: unused at the plaintext rung; present for toggle uniformity.
    :ivar include_siblings: unused at the plaintext rung; present for uniformity.
    :ivar include_gist: unused at the plaintext rung; present for uniformity.
    :ivar last_reasoning: the agent's most-recent stated semantic goal, rendered as
        ``why: <text>`` above the anchor whenever set.
    """

    cursor_id: str
    relative_path: str
    trail: list[str] = field(default_factory=list)
    include_body: bool = True
    include_chain: bool = False
    include_trail: bool = False
    include_siblings: bool = False
    include_gist: bool = False
    last_reasoning: str | None = None


AnyCursorState = CursorState | StructuralCursorState | PlaintextCursorState


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


def _format_loc_str(relative_path: str | None, line: int | None) -> str:
    """Format a ``file:line`` cite for the symbolic projection.

    Returns ``"?"`` when no location information is available, the bare
    relative path when the line is unknown, and ``"path:line"`` (1-based,
    ``cat -n`` equivalent) when both are present. ``line`` is the internal
    0-based index; it is converted to 1-based here at the display boundary.
    """
    if relative_path and line is not None:
        return f"{relative_path}:{to_display_line(line)}"
    if relative_path:
        return relative_path
    return "?"


def _gist_from_body(body: str | None) -> str | None:
    """Extract a one-sentence gist from a symbol body.

    Walks the body line by line, skipping decorators, signature lines
    (``def`` / ``class`` / ``async def``), and bracket-only delimiters,
    returning the first substantive content stripped of leading and
    trailing docstring-quote characters and capped at
    :data:`_GIST_MAX_CHARS`. Returns ``None`` when no substantive line
    is found so callers can omit the gist row entirely.
    """
    if not body:
        return None
    skip_prefixes = ("def ", "async def ", "class ", "@", "function ", "interface ", "struct ", "enum ")
    bracket_only = {"{", "}", "(", ")", "[", "]"}
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped in bracket_only:
            continue
        if stripped.startswith(skip_prefixes):
            continue
        # peel docstring openers: triple-quote first (single-line ``"""x"""``),
        # then any leftover loose quotes / comment markers
        cleaned = stripped
        if cleaned.startswith(('"""', "'''")):
            cleaned = cleaned[3:]
        if cleaned.endswith(('"""', "'''")):
            cleaned = cleaned[:-3]
        cleaned = cleaned.lstrip("\"'").rstrip("\"'").lstrip("#").strip()
        # if peeling left only a quote remnant (e.g. ``"""`` alone), continue
        if not cleaned or cleaned in {'"""', "'''"}:
            continue
        return cleaned[:_GIST_MAX_CHARS]
    return None


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
        # the universal plaintext floor (spec-v2 §5.1 rung3): renders any file the
        # LSP and structural rungs do not claim; the manager owns the byte read
        self._plaintext_floor = default_plaintext_floor()

    @property
    def project(self) -> Project:
        """:return: the project this manager is bound to (read-only)."""
        return self._project

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

        Raises :class:`TypeError` when the cursor is a non-LSP cursor -- a
        :class:`StructuralCursorState` (container member) or a
        :class:`PlaintextCursorState` (whole file at the floor rung). Callers
        that can only operate on the language server's symbol graph use this
        accessor to fail fast with a typed error that names the cursor kind and
        points at the right tool, never a raw ``AttributeError`` (spec-v2 §5.1:
        no rung raises a terminal error to the agent).
        """
        state = self.get_cursor(cursor_id)
        if isinstance(state, CursorState):
            return state
        if isinstance(state, PlaintextCursorState):
            raise TypeError(
                f"Cursor '{cursor_id}' is a plaintext (whole-file) cursor at "
                f"{state.relative_path}; a whole-file cursor has no symbol graph to "
                f"navigate or rename. Edit it by line via cursor_replace_range, or "
                f"cursor_start on a symbol to obtain an LSP cursor.",
            )
        raise TypeError(
            f"Cursor '{cursor_id}' is a structural cursor at "
            f"{state.relative_path}:{state.name_path!r}; this operation requires an LSP cursor",
        )

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
            with the empty :data:`DEFAULT_EDGE_TYPES` set -- the cursor
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
                # plaintext floor (spec-v2 §5.1 rung3): neither the language server
                # nor a structural backend claims this file -> land a whole-file
                # plaintext cursor rather than dead-ending, provided the file exists.
                view = self._plaintext_view(relative_path)
                if not view.exists:
                    raise lsp_error
                floor_id = cursor_id if cursor_id is not None else self._generate_cursor_id()
                plain_state = PlaintextCursorState(cursor_id=floor_id, relative_path=relative_path)
                self._cursors[floor_id] = plain_state
                return floor_id, plain_state
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
            # Multiple candidates -- try exact name match
            exact = [n for n in candidates if n.name == target_name]
            if len(exact) == 1:
                candidate = exact[0]
            else:
                names = [f"  {n.format_compact()}" for n in candidates]
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
        # plaintext cursors address a whole file at the floor rung; they expose no
        # navigable neighbors (moving line-to-line is line-addressed, not a graph)
        if isinstance(state, PlaintextCursorState):
            return []
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

        # per-edge timing: lets developers diagnose which LSP edges dominate
        # wall-clock cost on a given project (SourceKit-LSP REFERENCES /
        # INHERITS can be multi-minutes per symbol on large indexed
        # projects). Each edge logs its elapsed wall-clock + neighbor count
        # at INFO level under the ``cursor.lsp_timing`` logger so callers
        # can grep one channel without filter noise.
        timing_log = logging.getLogger("cursor.lsp_timing")
        per_edge_timing: dict[EdgeType, float] = {}
        per_edge_counts: dict[EdgeType, int] = {}

        def _record_edge(edge: EdgeType, started_at: float, count: int) -> None:
            elapsed = time.perf_counter() - started_at
            per_edge_timing[edge] = elapsed
            per_edge_counts[edge] = count
            timing_log.info(
                "cursor=%s edge=%s elapsed_ms=%.1f neighbors=%d",
                cursor_id,
                edge.value,
                elapsed * 1000.0,
                count,
            )

        total_started = time.perf_counter()

        # Contains: children of the current symbol (in-process iteration; no LSP roundtrip)
        if EdgeType.CONTAINS in state.active_edge_types:
            t0 = time.perf_counter()
            n_before = len(neighbors)
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
            _record_edge(EdgeType.CONTAINS, t0, len(neighbors) - n_before)

        retriever = self._retriever
        ls = retriever.get_language_server(rel_path)
        failed_edge_types: list[EdgeType] = []

        # References: symbols that THIS symbol references (definitions it points to)
        if EdgeType.REFERENCES in state.active_edge_types:
            t0 = time.perf_counter()
            n_before = len(neighbors)
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
            _record_edge(EdgeType.REFERENCES, t0, len(neighbors) - n_before)

        # Referenced-by: symbols that reference THIS symbol
        if EdgeType.REFERENCED_BY in state.active_edge_types:
            t0 = time.perf_counter()
            n_before = len(neighbors)
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
            _record_edge(EdgeType.REFERENCED_BY, t0, len(neighbors) - n_before)

        # Calls: symbols that this symbol calls (outgoing calls)
        if EdgeType.CALLS in state.active_edge_types:
            t0 = time.perf_counter()
            n_before = len(neighbors)
            try:
                outgoing = ls.request_call_hierarchy_outgoing(rel_path, line, col)
                for outgoing_call in outgoing:
                    target = outgoing_call["to"]
                    neighbors.append(self._neighbor_from_hierarchy_item(target, EdgeType.CALLS))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve outgoing calls for cursor: {e}")
                failed_edge_types.append(EdgeType.CALLS)
            _record_edge(EdgeType.CALLS, t0, len(neighbors) - n_before)

        # Called-by: symbols that call this symbol (incoming calls)
        if EdgeType.CALLED_BY in state.active_edge_types:
            t0 = time.perf_counter()
            n_before = len(neighbors)
            try:
                incoming = ls.request_call_hierarchy_incoming(rel_path, line, col)
                for incoming_call in incoming:
                    caller = incoming_call["from"]
                    neighbors.append(self._neighbor_from_hierarchy_item(caller, EdgeType.CALLED_BY))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve incoming calls for cursor: {e}")
                failed_edge_types.append(EdgeType.CALLED_BY)
            _record_edge(EdgeType.CALLED_BY, t0, len(neighbors) - n_before)

        # Inherits: supertypes of the current symbol
        if EdgeType.INHERITS in state.active_edge_types:
            t0 = time.perf_counter()
            n_before = len(neighbors)
            try:
                supertypes = ls.request_type_hierarchy_supertypes(rel_path, line, col)
                for item in supertypes:
                    neighbors.append(self._neighbor_from_type_hierarchy_item(item, EdgeType.INHERITS))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve supertypes for cursor: {e}")
                failed_edge_types.append(EdgeType.INHERITS)
            _record_edge(EdgeType.INHERITS, t0, len(neighbors) - n_before)

        # Inherited-by: subtypes of the current symbol
        if EdgeType.INHERITED_BY in state.active_edge_types:
            t0 = time.perf_counter()
            n_before = len(neighbors)
            try:
                subtypes = ls.request_type_hierarchy_subtypes(rel_path, line, col)
                for item in subtypes:
                    neighbors.append(self._neighbor_from_type_hierarchy_item(item, EdgeType.INHERITED_BY))
            except SolidLSPException as e:
                log.debug(f"Failed to resolve subtypes for cursor: {e}")
                failed_edge_types.append(EdgeType.INHERITED_BY)
            _record_edge(EdgeType.INHERITED_BY, t0, len(neighbors) - n_before)

        if failed_edge_types:
            names = ", ".join(e.value for e in failed_edge_types)
            log.warning(f"Cursor {cursor_id}: {len(failed_edge_types)} edge type(s) failed to resolve: {names}")

        # summary line: total wall-clock + per-edge breakdown so a single
        # ``cursor.lsp_timing`` log entry captures the whole resolution
        total_elapsed = time.perf_counter() - total_started
        if per_edge_timing:
            breakdown = ", ".join(
                f"{e.value}={per_edge_timing[e] * 1000.0:.1f}ms/{per_edge_counts[e]}"
                for e in EdgeType
                if e in per_edge_timing
            )
            timing_log.info(
                "cursor=%s resolve_neighbors total_ms=%.1f [%s]",
                cursor_id,
                total_elapsed * 1000.0,
                breakdown,
            )

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
        backend = self._structural_registry.structural_backend_for(state.relative_path)
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
            log.debug(f"Could not resolve symbol name at {relative_path}:{to_display_line(line)}: {e}")
        return f"{os.path.basename(relative_path)}:{to_display_line(line)}"

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
        """Render the cursor as a compact symbolic projection.

        The projection is built from layered facts; only the **anchor**
        and **edge blocks** render unconditionally. Every other layer is
        gated by an ``include_*`` toggle on the cursor state so a fresh
        cursor projects just its position by default. When the cursor's
        ``last_reasoning`` is set, a ``why: <text>`` line is rendered
        ABOVE the anchor so the agent's stated semantic goal frames every
        view of the cursor:

        * **Why** -- ``why: <reasoning>`` -- the agent's most-recent
          stated semantic goal. Rendered above the anchor. Always shown
          when ``state.last_reasoning`` is set.
        * **Anchor** -- ``@ name :Kind@file:start-end:`` placing the
          cursor on a stable handle that includes its body extent.
          Always rendered.
        * **Edge blocks** -- one block per active edge type that produced
          neighbors. Outgoing edges (calls, references, inherits) carry a
          ``->`` arrow, incoming (called-by, referenced-by,
          inherited-by) a ``<-`` arrow. ``contains`` collapses to a
          single inline list. Rendered when ``state.active_edge_types``
          is non-empty.
        * **Trail** -- last-N prior hops with ``<- here`` marking the
          current position. Gated by ``state.include_trail``.
        * **Chain** -- ascending hierarchy from the immediate enclosing
          symbol up to the file. Gated by ``state.include_chain``.
        * **Siblings** -- peer names alongside the current symbol. Gated
          by ``state.include_siblings``.
        * **Gist** -- one-sentence body extract. Gated by
          ``state.include_gist``.

        The optional ``--- body ---`` block is appended when
        ``state.include_body`` is set, preserving the existing opt-in for
        full source.

        :param cursor_id: the cursor to format.
        :return: the multi-line projection.
        """
        state = self.get_cursor(cursor_id)
        if isinstance(state, PlaintextCursorState):
            return self._format_plaintext_cursor_view(state)
        if isinstance(state, StructuralCursorState):
            return self._format_structural_cursor_view(state)

        symbol = state.current_symbol
        location = state.current_location

        lines: list[str] = []

        # why: agent's stated semantic goal, rendered above the anchor so the
        # judge / reader always sees intent before observation
        if state.last_reasoning:
            lines.append(f"why: {state.last_reasoning}")
            lines.append("")

        # anchor: the cursor's own handle with body extent (always rendered)
        lines.append(self._render_anchor(symbol, location))

        # trail: prior hops + current marked '<- here'; opt-in via include_trail
        if state.include_trail:
            trail_block = self._render_trail(state)
            if trail_block:
                lines.append("")
                lines.extend(trail_block)

        # chain: ascending hierarchy ending at the file; opt-in via include_chain
        if state.include_chain:
            chain_block = self._render_chain(symbol, location)
            if chain_block:
                lines.append("")
                lines.extend(chain_block)

        # edge blocks: only when the cursor opted into edges
        if state.active_edge_types:
            neighbors = self.resolve_neighbors(cursor_id)
            neighbors_by_edge: dict[EdgeType, list[NeighborSymbol]] = {}
            for n in neighbors:
                neighbors_by_edge.setdefault(n.edge_type, []).append(n)
            for edge_type in EdgeType:
                edge_neighbors = neighbors_by_edge.get(edge_type)
                if not edge_neighbors:
                    continue
                lines.append("")
                lines.extend(self._render_edge_block(edge_type, edge_neighbors))

        # siblings: peer symbols at the same level; opt-in via include_siblings
        if state.include_siblings:
            sibling_line = self._render_siblings(symbol, location)
            if sibling_line:
                lines.append("")
                lines.append(sibling_line)

        # gist: first substantive body line; opt-in via include_gist
        if state.include_gist:
            gist_line = self._render_gist(symbol)
            if gist_line:
                lines.append("")
                lines.append(gist_line)

        # body block (opt-in): full statement-widened body for symbols whose
        # LSP extent is name-only; falls back to the LSP-reported body. Each
        # line is prefixed with its 1-based file line number so a non-symbol
        # region (e.g. a switch case) can be anchored for cursor_replace_range
        # without hand-counting -- bug://serena/cursor-edit-non-symbol-regions-need-line-numbers
        if state.include_body:
            body_text = self._format_widened_body(symbol)
            if body_text is None:
                body_text = symbol.body
            if body_text:
                body_start = symbol.get_body_start_position()
                start_line = body_start.line if body_start is not None else None
                lines.append("")
                lines.append("--- body ---")
                lines.extend(self._number_body_lines(body_text, start_line))
                lines.append("--- end body ---")

        return "\n".join(lines)

    def _render_anchor(self, symbol: LanguageServerSymbol, location: LanguageServerSymbolLocation) -> str:
        """Render the cursor anchor as ``@ name :Kind@file:start-end:``.

        The end line is the body end position (when available); when only
        the start line is known the form collapses to ``@ name :Kind@file:line:``.
        """
        name_path = symbol.get_name_path()
        kind = symbol.symbol_kind_name
        rel = location.relative_path
        if rel is None or location.line is None:
            cite = _format_loc_str(rel, location.line)
            return f"@ {name_path} :{kind}@{cite}:"
        start_line = location.line
        end_pos = symbol.body_end_position
        end_line = end_pos["line"] if end_pos else None
        if end_line is None or end_line == start_line:
            return f"@ {name_path} :{kind}@{rel}:{to_display_line(start_line)}:"
        return f"@ {name_path} :{kind}@{rel}:{format_line_range(start_line, end_line)}:"

    def _render_trail(self, state: CursorState) -> list[str]:
        """Render the cursor's trail block, last-N prior hops + ``<- here`` marker.

        Returns an empty list when the trail is empty so the caller skips
        the section header entirely. Each hop is rendered as
        ``name :Kind@file:line:``; the current position is the final entry
        with the ``<- here`` marker appended.
        """
        if not state.trail:
            return []
        lines = ["trail"]
        for entry in state.trail[-_TRAIL_TAIL_LENGTH:]:
            cite = _format_loc_str(entry.relative_path, entry.line)
            if entry.kind:
                lines.append(f"   {entry.name} :{entry.kind}@{cite}:")
            else:
                lines.append(f"   {entry.name} @{cite}:")
        # current position with the '<- here' marker so readers can locate themselves
        cur_cite = _format_loc_str(state.current_location.relative_path, state.current_location.line)
        cur_kind = state.current_symbol.symbol_kind_name
        cur_name = state.current_symbol.name
        if cur_kind:
            lines.append(f"   {cur_name} :{cur_kind}@{cur_cite}:    <- here")
        else:
            lines.append(f"   {cur_name} @{cur_cite}:    <- here")
        return lines

    def _render_chain(self, symbol: LanguageServerSymbol, location: LanguageServerSymbolLocation) -> list[str]:
        """Render the ascending containment chain up to (and including) the file.

        Walks ``symbol.iter_ancestors(up_to_symbol_kind=SymbolKind.File)``
        for symbol-level ancestors, then appends the file path itself as
        the outermost frame. Returns an empty list when nothing is known
        about the symbol's location.
        """
        lines: list[str] = []
        for ancestor in symbol.iter_ancestors(up_to_symbol_kind=SymbolKind.File):
            anc_kind = ancestor.symbol_kind_name
            anc_loc = ancestor.location
            cite = _format_loc_str(anc_loc.relative_path, anc_loc.line)
            if anc_kind:
                lines.append(f"   <- {ancestor.name} :{anc_kind}@{cite}:")
            else:
                lines.append(f"   <- {ancestor.name} @{cite}:")
        # file frame: the bottommost <- entry, no line number
        if location.relative_path:
            lines.append(f"   <- {location.relative_path}")
        elif not lines:
            return []
        return ["chain", *lines]

    def _render_edge_block(self, edge_type: EdgeType, neighbors: list[NeighborSymbol]) -> list[str]:
        """Render one edge type's neighbors as a block.

        ``contains`` collapses to a single inline ``contains v  a, b, c``
        line because membership is high-cardinality and rarely benefits
        from per-entry locations. Outgoing edges (calls, references,
        inherits) carry a ``->`` arrow header; incoming (called-by,
        referenced-by, inherited-by) a ``<-``. Each entry under those
        renders on a single line via :meth:`NeighborSymbol.format_compact`.
        """
        if edge_type == EdgeType.CONTAINS:
            names = [n.name for n in neighbors[:_MAX_INLINE_LIST]]
            tail = "" if len(neighbors) <= _MAX_INLINE_LIST else f", ... (+{len(neighbors) - _MAX_INLINE_LIST})"
            return [f"contains v  {', '.join(names)}{tail}"]

        outgoing = {EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.INHERITS}
        arrow = "->" if edge_type in outgoing else "<-"
        header = f"{edge_type.value} {arrow}"
        block = [header]
        for n in neighbors:
            block.append(f"   {n.format_compact()}")
        return block

    def _render_siblings(self, symbol: LanguageServerSymbol, location: LanguageServerSymbolLocation) -> str | None:
        """Render an inline list of peer names, or ``None`` when no parent is known.

        Filters the parent's children by (name, line) so the cursor's own
        symbol is excluded even when overload indices share the same
        plain name. Caps the list at :data:`_MAX_INLINE_LIST` and appends
        ``(+N)`` when more peers exist.
        """
        parent = symbol.get_parent()
        if parent is None:
            return None
        names: list[str] = []
        skipped = 0
        cur_name = symbol.name
        cur_line = location.line
        for child in parent.iter_children():
            # exclude self by (name, line) tuple so overload-distinct siblings still surface
            if child.name == cur_name and child.line == cur_line:
                continue
            if len(names) >= _MAX_INLINE_LIST:
                skipped += 1
                continue
            names.append(child.name)
        if not names:
            return None
        tail = "" if skipped == 0 else f", ... (+{skipped})"
        return f"siblings    {', '.join(names)}{tail}"

    def _render_gist(self, symbol: LanguageServerSymbol) -> str | None:
        """Render the symbol's gist, or ``None`` when no body is available."""
        gist = _gist_from_body(symbol.body)
        if gist is None:
            return None
        return f"gist        {gist}"

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

    def _number_body_lines(self, body_text: str, start_line: int | None) -> list[str]:
        """Prefix each line of a body projection with its 1-based file line number.

        The numbers let an agent anchor ``cursor_replace_range`` on a non-symbol
        region inside the body (e.g. a switch ``case``) without hand-counting from
        the anchor range. ``start_line`` is the internal 0-based file line at which
        ``body_text`` begins (the symbol's body-start line); it is converted to the
        1-based ``cat -n`` number at this display boundary. When it is ``None`` the
        line numbers cannot be known, so the text is returned unchanged rather than
        fabricating numbers.

        :param body_text: the body source text (possibly multi-line).
        :param start_line: the 0-based file line of ``body_text``'s first line, or
            ``None`` when unavailable.
        :return: the body as a list of lines, each numbered (1-based) when
            ``start_line`` is known.
        """
        if start_line is None:
            return [body_text]
        return [f"{to_display_line(start_line + i)}: {line}" for i, line in enumerate(body_text.splitlines())]

    def _format_structural_cursor_view(self, state: StructuralCursorState) -> str:
        """Render a structural cursor's position as a compact symbolic projection.

        Mirrors :meth:`format_cursor_view` for non-LSP cursors: the LSP
        graph is empty, so calls/references/inheritance are absent, but
        the cursor still gets an anchor (always), a contains list (when
        the addressed node has members), and -- gated by the same
        ``include_*`` toggles as the LSP path -- a trail, chain,
        siblings, and gist. The agent's ``last_reasoning`` is rendered
        above the anchor whenever set, mirroring the LSP path.
        """
        lines: list[str] = []

        # why: agent's stated semantic goal, rendered above the anchor
        if state.last_reasoning:
            lines.append(f"why: {state.last_reasoning}")
            lines.append("")

        # anchor: a structural node carries a line range only when its backend
        # exposes one (the tree-sitter rung does; the thirteen AST-editor backends
        # return None and the anchor stays line-less). When present it renders
        # 1-based (cat -n) via the T3 converter, so the ts rung never leaks a
        # 0-based line (spec-v2 §5.7/§5.8).
        kind_str = state.kind or ""
        line_range = self._structural_node_line_range(state)
        range_suffix = f"{format_line_range(*line_range)}:" if line_range is not None else ""
        if kind_str:
            lines.append(f"@ {state.name_path} :{kind_str}@{state.relative_path}:{range_suffix}")
        else:
            lines.append(f"@ {state.name_path} @{state.relative_path}:{range_suffix}")

        # trail: prior name_paths + current marked '<- here'; opt-in
        if state.include_trail and state.trail:
            lines.append("")
            lines.append("trail")
            for prior_path in state.trail[-_TRAIL_TAIL_LENGTH:]:
                lines.append(f"   {prior_path} @{state.relative_path}:")
            lines.append(f"   {state.name_path} @{state.relative_path}:    <- here")

        # chain: ascending name-path segments ending at the file; opt-in
        if state.include_chain:
            chain_block = self._render_structural_chain(state)
            if chain_block:
                lines.append("")
                lines.extend(chain_block)

        # contains: structural members of the addressed node, inline (always rendered when present)
        children = self._resolve_structural_neighbors(state)
        if children:
            lines.append("")
            names = [c.name for c in children[:_MAX_INLINE_LIST]]
            tail = "" if len(children) <= _MAX_INLINE_LIST else f", ... (+{len(children) - _MAX_INLINE_LIST})"
            lines.append(f"contains v  {', '.join(names)}{tail}")

        # siblings: peer members under the same parent name path; opt-in
        if state.include_siblings:
            sibling_line = self._render_structural_siblings(state)
            if sibling_line:
                lines.append("")
                lines.append(sibling_line)

        # gist: first substantive line of the serialized node text; opt-in
        body_text = self._format_structural_node_source(state)
        if state.include_gist:
            gist = _gist_from_body(body_text)
            if gist:
                lines.append("")
                lines.append(f"gist        {gist}")

        # body block (opt-in): the addressed node's serialized source
        if state.include_body and body_text is not None:
            lines.append("")
            lines.append("--- body ---")
            lines.append(body_text)
            lines.append("--- end body ---")

        return "\n".join(lines)

    def _plaintext_view(self, relative_path: str) -> PlaintextView:
        """Read a file's raw bytes and render the plaintext-floor view; never raise.

        The manager owns the byte-access boundary, so the read happens here and the
        pure :class:`~solidlsp.structural.backends.plaintext.PlaintextFloor` renders
        the result. A missing or unreadable file becomes a typed view (spec-v2 §5.9),
        never an exception -- so no floor lookup dead-ends.

        :param relative_path: project-relative path of the file to read.
        :return: the file's :class:`PlaintextView`.
        """
        abs_path = os.path.join(self._project.project_root, relative_path)
        try:
            with open(abs_path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._plaintext_floor.not_found_view(relative_path)
        except OSError as e:
            return self._plaintext_floor.error_view(relative_path, str(e))
        return self._plaintext_floor.render(data, relative_path)

    def plaintext_overview(self, relative_path: str) -> str:
        """Return the plaintext floor's one-line summary for ``relative_path``.

        Used by ``cursor_overview``'s rung-3 branch so a file no LSP or structural
        rung claims still yields a line/size/encoding descriptor instead of the old
        "Cannot extract symbols" dead-end (spec-v2 §5.1 rung3). Never raises.

        :param relative_path: project-relative path of the file to summarize.
        :return: the descriptor line (e.g. ``"42 lines, 1310 bytes, utf-8, LF, trailing newline"``).
        """
        return self._plaintext_floor.describe(self._plaintext_view(relative_path))

    def _format_plaintext_cursor_view(self, state: PlaintextCursorState) -> str:
        """Render a plaintext cursor's position: why + anchor + descriptor + body.

        Mirrors :meth:`_format_structural_cursor_view` for the floor rung. The anchor
        carries the file's descriptor (line/size/encoding); the byte-exact numbered
        body follows when ``include_body`` is set, its line numbers routed through the
        1-based display converter (spec-v2 §5.7) so they agree with cat -n. The file
        is re-read on each projection so concurrent on-disk edits are reflected.
        """
        view = self._plaintext_view(state.relative_path)
        lines: list[str] = []

        # why: agent's stated semantic goal, rendered above the anchor
        if state.last_reasoning:
            lines.append(f"why: {state.last_reasoning}")
            lines.append("")

        # anchor: the file handle carrying its plaintext descriptor
        kind = "binary" if view.is_binary else "file"
        lines.append(f"@ {state.relative_path} :{kind}@{state.relative_path}:  {self._plaintext_floor.describe(view)}")

        # body block (opt-in, default on): the file's byte-exact numbered body,
        # numbered from file line 0 through the 1-based display converter
        if state.include_body and view.text is not None:
            lines.append("")
            lines.append("--- body ---")
            lines.extend(self._number_body_lines(view.text, 0))
            lines.append("--- end body ---")

        return "\n".join(lines)

    def _render_structural_chain(self, state: StructuralCursorState) -> list[str]:
        """Render a structural cursor's chain by walking up its name-path segments.

        Each ancestor name path resolves through the structural cache so
        its kind decorates the chain entry. The final frame is the file
        path itself, mirroring :meth:`_render_chain`.
        """
        backend = self._structural_registry.structural_backend_for(state.relative_path)
        cache_entry = self._structural_cache_entry(backend, state.relative_path) if backend is not None else None
        ancestor_paths: list[str] = []
        path = _parent_name_path(state.name_path)
        while path is not None:
            ancestor_paths.append(path)
            path = _parent_name_path(path)
        # render immediate parent first, root last (mirroring iter_ancestors order)
        chain_lines: list[str] = []
        for anc_path in ancestor_paths:
            kind: str = ""
            if cache_entry is not None:
                match = cache_entry.nodes_by_path.get(anc_path)
                if match is not None:
                    kind = match[0]
            if kind:
                chain_lines.append(f"   <- {anc_path} :{kind}@{state.relative_path}:")
            else:
                chain_lines.append(f"   <- {anc_path} @{state.relative_path}:")
        # file frame
        chain_lines.append(f"   <- {state.relative_path}")
        return ["chain", *chain_lines]

    def _render_structural_siblings(self, state: StructuralCursorState) -> str | None:
        """Render peer members under the structural cursor's parent path."""
        parent_path = _parent_name_path(state.name_path)
        if parent_path is None:
            return None
        backend = self._structural_registry.structural_backend_for(state.relative_path)
        if backend is None:
            return None
        cache_entry = self._structural_cache_entry(backend, state.relative_path)
        if cache_entry is None:
            return None
        parent_segment_count = len(_split_name_path_segments(parent_path))
        peers: list[str] = []
        skipped = 0
        for candidate_path in cache_entry.nodes_by_path:
            if not candidate_path.startswith(parent_path + "/"):
                continue
            if len(_split_name_path_segments(candidate_path)) != parent_segment_count + 1:
                continue
            if candidate_path == state.name_path:
                continue
            if len(peers) >= _MAX_INLINE_LIST:
                skipped += 1
                continue
            peers.append(candidate_path)
        if not peers:
            return None
        tail = "" if skipped == 0 else f", ... (+{skipped})"
        return f"siblings    {', '.join(peers)}{tail}"

    def _format_structural_node_source(self, state: StructuralCursorState) -> str | None:
        """Return the serialized source text for the node at ``state.name_path``.

        Returns ``None`` when the backend cannot render the addressed node as
        a standalone source string -- which today covers the ``container``
        wrapper yielded by Python's ``walk_container_members`` (a bare
        ``cst.Dict``/``cst.List`` has no standalone module serializer
        attached). Callers should treat ``None`` as "no body available".
        """
        backend = self._structural_registry.structural_backend_for(state.relative_path)
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

    def _structural_node_line_range(self, state: StructuralCursorState) -> tuple[int, int] | None:
        """Return the addressed node's 0-based inclusive ``(start, end)`` line span, or ``None``.

        Only a backend that exposes
        :meth:`~solidlsp.structural.base.StructuralLanguage.node_line_range` (the
        tree-sitter fallback rung) carries a line span; the thirteen AST-editor
        backends return ``None`` and the structural anchor stays line-less. The
        span is 0-based internally and converted to a 1-based ``cat -n`` range at
        the display boundary (spec-v2 §5.7/§5.8). Mirrors
        :meth:`_format_structural_node_source`'s node lookup and is best-effort:
        any backend hiccup omits the range rather than failing the view.
        """
        backend = self._structural_registry.structural_backend_for(state.relative_path)
        if backend is None:
            return None
        cache_entry = self._structural_cache_entry(backend, state.relative_path)
        if cache_entry is None:
            return None
        match = cache_entry.nodes_by_path.get(state.name_path)
        if match is None:
            return None
        _kind, node = match
        try:
            return backend.node_line_range(node)
        except Exception as e:
            log.debug(f"Could not compute structural node line range: {e}")
            return None

    @staticmethod
    def _serialize_node_for_display(backend: StructuralLanguage, node: Any) -> str:
        """Render ``node`` to standalone source via the backend's renderer.

        Rendering is the backend's responsibility
        (:meth:`~solidlsp.structural.base.StructuralLanguage.render_node_source`):
        a backend emits a walked node's VALUE, and one that cannot raises
        :class:`~solidlsp.structural.errors.NodeRenderError`, which
        :meth:`_format_structural_node_source` catches and reports as "no body
        available". An internal node repr never crosses this boundary.
        """
        return backend.render_node_source(node)

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

    def resolve_read_rung(
        self,
        relative_path: str,
        retriever: LanguageServerSymbolRetriever | None = None,
    ) -> ReadRung:
        """Resolve which rung of the read ladder serves ``relative_path`` (spec-v2 §5.1).

        The single ladder ``cursor_overview`` and ``cursor_grep`` consult so they
        never disagree with ``cursor_start`` about how a file is read:

        * :attr:`ReadRung.LSP` when the language server can analyze the file;
        * else :attr:`ReadRung.STRUCTURAL` when a structural backend is
          registered for the extension;
        * else :attr:`ReadRung.PLAINTEXT` -- the universal floor.

        Never raises: every path resolves to a rung.

        :param relative_path: project-relative path to classify.
        :param retriever: optional retriever to reuse (the grep loop passes its
            own so per-match classification does not re-instantiate one);
            defaults to a fresh :attr:`_retriever`.
        :return: the rung that serves the file.
        """
        retriever = retriever if retriever is not None else self._retriever
        if retriever.can_analyze_file(relative_path):
            return ReadRung.LSP
        if self._structural_registry.structural_backend_for(relative_path) is not None:
            return ReadRung.STRUCTURAL
        return ReadRung.PLAINTEXT

    def structural_overview(self, relative_path: str) -> list[tuple[str, KindName]]:
        """Return the file's TOP-LEVEL structural nodes as ``(name_path, kind)``.

        Used by ``cursor_overview``'s structural rung so a non-LSP file
        (yaml/json/toml/...) still yields a symbol listing instead of the old
        "Cannot extract symbols" dead-end. Returns the depth-1 nodes from the
        structural walk in document order. Empty list when no structural backend
        is registered for the file or the file cannot be read/parsed -- never
        raises (the caller falls through to the plaintext floor).

        :param relative_path: project-relative path of the file to summarize.
        :return: top-level ``(name_path, kind)`` pairs; empty when the file has
            no structural rung or cannot be parsed.
        """
        backend = self._structural_registry.structural_backend_for(relative_path)
        if backend is None:
            return []
        try:
            cache_entry = self._structural_cache_entry(backend, relative_path)
        except Exception as e:
            # parsing is best-effort: a malformed file falls through to the
            # floor rather than raising (spec-v2 §5.1: no rung raises)
            log.debug(f"structural_overview: could not parse {relative_path}: {e}")
            return []
        if cache_entry is None:
            return []
        top_level: list[tuple[str, KindName]] = []
        for name_path, (kind, _node) in cache_entry.nodes_by_path.items():
            if name_path and len(_split_name_path_segments(name_path)) == 1:
                top_level.append((name_path, kind))
        return top_level

    def find_pattern_with_enclosing_symbols(
        self,
        substring_pattern: str,
        relative_path: str | None = None,
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        restrict_to_code_files: bool = True,
        context_lines_before: int = 0,
        context_lines_after: int = 0,
    ) -> tuple[list[tuple[LanguageServerSymbol, list[str]]], list[tuple[str, list[str]]]]:
        """Find regex matches grouped by their enclosing LSP symbol.

        Each match is associated with the smallest LSP symbol that contains
        its hit line. Matches that fall outside any addressable LSP symbol
        -- a hit in a non-LSP file (yaml/LICENSE/...) or in a genuine
        non-symbol region (comment/import/blank line) of an LSP file -- are
        NOT dropped: they are surfaced as file-level blocks carrying their
        matched line + number + context (spec-v2 §5.1/§5.3), so the cursor
        surface serves the read itself instead of deferring to another tool.

        :param substring_pattern: regular expression compiled with
            ``re.DOTALL``; mirrors :class:`~serena.tools.file_tools.SearchForPatternTool`
            semantics.
        :param relative_path: search root relative to the project. ``None``
            (the default) searches every non-ignored file.
        :param paths_include_glob: glob pattern restricting the file set.
            Empty disables include filtering.
        :param paths_exclude_glob: glob pattern excluding files; takes
            precedence over ``paths_include_glob``. Empty disables exclude.
        :param restrict_to_code_files: when ``True`` (the default), the
            search is confined to files an analyser can address
            symbolically -- the only files where enclosing-symbol grouping
            is meaningful.
        :param context_lines_before: extra lines rendered alongside each
            hit's matched line in the returned display strings.
        :param context_lines_after: extra lines rendered alongside each
            hit's matched line in the returned display strings.
        :return: a 2-tuple ``(groups, unsymboled)``. ``groups`` is a list
            of ``(enclosing_symbol, hit_display_strings)`` tuples in
            discovery order -- one tuple per unique enclosing symbol (each
            anchors a cursor). ``unsymboled`` is a list of
            ``(relative_path, hit_display_strings)`` tuples in discovery
            order -- one per file with matches outside any addressable LSP
            symbol; their matched lines are surfaced verbatim rather than
            reduced to a count.
        :raises FileNotFoundError: when ``relative_path`` does not exist
            on disk.
        """
        # locate and validate the search scope
        rel = relative_path or ""
        abs_path = os.path.join(self._project.project_root, rel)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Relative path {rel!r} does not exist.")

        # delegate the regex pass to the project search machinery so the
        # result set matches what SearchForPatternTool would surface
        if restrict_to_code_files:
            matches = self._project.search_source_files_for_pattern(
                pattern=substring_pattern,
                relative_path=rel,
                context_lines_before=context_lines_before,
                context_lines_after=context_lines_after,
                paths_include_glob=paths_include_glob.strip() or None,
                paths_exclude_glob=paths_exclude_glob.strip() or None,
            )
        else:
            from serena.util.file_system import scan_directory
            from serena.util.text_utils import search_files
            if os.path.isfile(abs_path):
                rel_paths_to_search = [rel]
            else:
                _dirs, rel_paths_to_search = scan_directory(
                    path=abs_path,
                    recursive=True,
                    is_ignored_dir=self._project.is_ignored_path,
                    is_ignored_file=self._project.is_ignored_path,
                    relative_to=self._project.project_root,
                )
            matches = search_files(
                rel_paths_to_search,
                substring_pattern,
                context_lines_before=context_lines_before,
                context_lines_after=context_lines_after,
                file_reader=self._project.read_file,
                root_path=self._project.project_root,
                paths_include_glob=paths_include_glob or None,
                paths_exclude_glob=paths_exclude_glob or None,
            )

        # resolve enclosing LSP symbol per match; first occurrence per
        # (rel_path, name_path) becomes the group's anchor symbol so the
        # caller can register a cursor at it
        retriever = self._retriever
        groups: dict[tuple[str, str], LanguageServerSymbol] = {}
        hits_per_group: dict[tuple[str, str], list[str]] = {}
        group_order: list[tuple[str, str]] = []
        # non-symbol hits are SURFACED as file-level blocks rather than dropped
        # (spec-v2 §5.1/§5.3): a hit in a non-LSP file (yaml/LICENSE/...) or in a
        # genuine non-symbol region (comment/import/blank) of an LSP file keeps
        # its matched line+number+context instead of being reduced to a count.
        unsymboled: dict[str, list[str]] = {}
        unsymboled_order: list[str] = []

        def _record_unsymboled(path: str, display: str) -> None:
            if path not in unsymboled:
                unsymboled[path] = []
                unsymboled_order.append(path)
            unsymboled[path].append(display)

        for match in matches:
            assert match.source_file_path is not None
            rel_path = match.source_file_path
            # MatchedConsecutiveLines.line_number is already 0-indexed --
            # same convention as the LSP query, so no conversion is needed
            matched_line = match.matched_lines[0]
            line_0idx = matched_line.line_number
            # query at the first non-whitespace column so the LSP's
            # innermost-container lookup lands on actual code -- column 0
            # of an indented line falls in the leading-whitespace gutter
            # and some LSP implementations return None for it
            line_content = matched_line.line_content or ""
            stripped = line_content.lstrip()
            col_0idx = len(line_content) - len(stripped) if stripped else 0
            # rung check via the ONE ladder resolver: only the LSP rung can
            # anchor a hit to an enclosing symbol. A structural/plaintext file
            # still surfaces its matched line as a file-level hit (spec §5.1/§5.3).
            if self.resolve_read_rung(rel_path, retriever=retriever) is not ReadRung.LSP:
                _record_unsymboled(rel_path, match.to_display_string())
                continue
            try:
                ls = retriever.get_language_server(rel_path)
                sym_dict = ls.request_containing_symbol(rel_path, line_0idx, col_0idx, strict=False)
            except Exception as e:
                # rendering is best-effort: when the LSP cannot answer the
                # containment query for one file, skip the hit rather than
                # fail the whole search
                log.debug(f"Could not resolve containing symbol for {rel_path}:{line_0idx}: {e}")
                sym_dict = None
            if sym_dict is None:
                # request_containing_symbol(strict=False) returns None for a hit on a
                # top-level symbol's OWN declaration line: it is not inside any deeper
                # container. Recover that symbol from the file overview so the hit
                # attaches to it instead of being counted as unsymboled.
                # bug://serena/cursor-grep-skips-package-level-symbol-declarations
                sym = self._top_level_symbol_covering_line(rel_path, line_0idx)
                if sym is None:
                    # genuine non-symbol region inside an LSP file (comment,
                    # import, blank line): surface the matched line, don't drop it
                    _record_unsymboled(rel_path, match.to_display_string())
                    continue
            else:
                sym = LanguageServerSymbol(sym_dict)
            name_path = sym.get_name_path()
            key = (rel_path, name_path)
            if key not in groups:
                groups[key] = sym
                hits_per_group[key] = []
                group_order.append(key)
            hits_per_group[key].append(match.to_display_string())

        # build the ordered results preserving discovery order for both the
        # symbol-anchored groups and the file-level non-symbol blocks
        return (
            [(groups[k], hits_per_group[k]) for k in group_order],
            [(p, unsymboled[p]) for p in unsymboled_order],
        )

    def _top_level_symbol_covering_line(self, relative_path: str, line_0idx: int) -> LanguageServerSymbol | None:
        """Recover the top-level symbol whose extent covers a 0-based line.

        ``request_containing_symbol(strict=False)`` returns ``None`` for a regex hit
        landing on a package-level (top-level) symbol's own declaration line -- the
        line is the symbol's own extent, not the interior of any deeper container, so
        the LSP reports no *containing* symbol. This fallback consults the file
        overview and returns the narrowest top-level symbol whose body extent contains
        the line, letting :meth:`find_pattern_with_enclosing_symbols` anchor a cursor
        on a top-level var/const/type declaration instead of dropping the hit.

        :param relative_path: the file the hit is in (relative to the project root).
        :param line_0idx: the 0-based line of the hit.
        :return: the covering top-level symbol, or ``None`` when none covers the line
            (a genuine non-symbol region such as a comment or import).
        """
        try:
            overview = self._retriever.get_symbol_overview(relative_path)
        except Exception as e:
            # best-effort, mirroring the containment-query handling in the caller
            log.debug(f"Could not load symbol overview for {relative_path}: {e}")
            return None
        best: LanguageServerSymbol | None = None
        best_span: int | None = None
        for symbols in overview.values():
            for sym in symbols:
                start = sym.get_body_start_position()
                end = sym.get_body_end_position()
                if start is None or end is None:
                    continue
                if start.line <= line_0idx <= end.line:
                    span = end.line - start.line
                    if best is None or (best_span is not None and span < best_span):
                        best = sym
                        best_span = span
        return best

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
        ``Class/method/if_stmt#0`` resolve to the same dict -- the structural
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
        backend = self._structural_registry.structural_backend_for(relative_path)
        if backend is None:
            return None

        # look up against the per-file cache, re-parsing on cache miss or mtime change
        cache_entry = self._structural_cache_entry(backend, relative_path)
        if cache_entry is None:
            return None

        # O(1) dict lookup -- walk_nodes already produced canonical name paths
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
          ``"boolean"`` / ``"null"`` / ``"scalar"``) -- the value is not a
          container so no member can be inserted into it.
        * ``insert_before`` / ``insert_after`` / ``replace`` / ``remove`` on
          a cursor whose ``name_path`` has no parent segment -- there is no
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
        backend = self._structural_registry.structural_backend_for(state.relative_path)
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
        ``walk_nodes`` index after invalidating the per-file cache -- this
        catches the case where a container-member edit has rewritten the
        file on disk and we need the new node handle.

        :param cursor_id: the cursor to re-anchor
        :param name_path: override name path (e.g. after a rename); defaults to the current symbol's name path
        :param relative_path: override file path; defaults to the current cursor location's file
        :return: the updated cursor state (same kind as the stored one)
        """
        state = self.get_cursor(cursor_id)
        if isinstance(state, PlaintextCursorState):
            # plaintext cursors re-read the file on every projection, so there is
            # nothing to re-anchor
            return state
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
        if isinstance(state, PlaintextCursorState):
            # a whole-file plaintext cursor never navigates (cursor_move rejects it),
            # so its trail is always empty
            return f"Cursor {cursor_id}: no trail (at starting position)"
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
        for i, entry in enumerate(state.trail):
            loc_str = _format_loc_str(entry.relative_path, entry.line)
            lines.append(f"  {i + 1}. {loc_str}")

        # Current position
        cur = state.current_location
        cur_str = _format_loc_str(cur.relative_path, cur.line)
        lines.append(f"  -> {cur_str} (current)")

        return "\n".join(lines)
