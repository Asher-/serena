"""Tree-sitter universal structural fallback backend (spec-v2 §5.8).

The bottom *structural* rung. For any file no LSP server and none of the thirteen
explicit :class:`~solidlsp.structural.base.StructuralLanguage` backends claim,
this backend reads it through the tree-sitter grammar ``tree-sitter-language-pack``
ships for the file's language, so the cursor surface exposes addressable structure
and exact-byte bodies instead of dropping straight to the plaintext floor.

READ + byte-span-write ONLY. The read half of the ABC is implemented -- ``parse`` /
``serialize`` (byte-identical round-trip), a ``walk_symbols`` over the named-node
tree, ``render_node_source`` as an exact byte slice, and the ``node_line_range``
hook -- while every structural-mutation method raises :class:`NotImplementedError`:
arbitrary re-emission on a tree-sitter-only language is out of cut one (spec-v2 §5.8),
and byte-span writes go through ``cursor_replace_range``, which is line-based and
never touches the backend.

The installed ``tree_sitter_language_pack`` binding is **method-based**
(``node.kind()``, ``node.start_byte()``, ``node.start_position().row``) and offers
no incremental reparse, so this backend does a full parse on every call; the
cursor's mtime-keyed structural cache already absorbs that (there is no per-tree
state to invalidate incrementally). ``parser.parse`` takes a single ``str`` and
never raises on malformed input -- it produces ERROR / MISSING nodes -- so a parse
failure is surfaced as :attr:`_TreeSitterTree.has_error`, never an exception.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from tree_sitter_language_pack import get_parser

from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.errors import NameResolutionError
from solidlsp.structural.kinds import KindName, KindSchema
from solidlsp.structural.names import LogicalName, LogicalNameResolver, NameResolution

# --------------------------------------------------------------------------- #
# Language routing -- ONLY extensions/basenames the explicit thirteen do not own
# (the explicit registry backends win before this resolver is consulted, so an
# overlap here would never be reached; keeping it disjoint documents intent).
# --------------------------------------------------------------------------- #

_TS_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".sh": "bash",
    ".bash": "bash",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".sql": "sql",
    ".ini": "ini",
    ".cfg": "ini",
    ".xml": "xml",
    ".css": "css",
    ".html": "html",
    ".htm": "html",
    ".proto": "proto",
}
_TS_LANGUAGE_BY_BASENAME: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Makefile": "make",
    "makefile": "make",
}

# how many named levels below the root the walk descends: enough to expose
# addressable structure with exact bodies, not a full syntax dump
_WALK_MAX_DEPTH = 2


def _language_name_for_path(relative_path: str) -> str | None:
    """Map a path to a tree-sitter language name, or ``None`` when unmapped.

    Suffix first, then bare basename (``Dockerfile`` / ``Makefile`` carry no
    suffix). Only NEW mappings appear here.
    """
    pure = PurePosixPath(relative_path)
    suffix = pure.suffix.lower()
    if suffix in _TS_LANGUAGE_BY_SUFFIX:
        return _TS_LANGUAGE_BY_SUFFIX[suffix]
    return _TS_LANGUAGE_BY_BASENAME.get(pure.name)


# one backend instance per language, memoized module-wide: the registry hands back
# a per-language-bound backend because the ABC ``parse(source)`` has no path to
# route on, so the language must be fixed at construction.
_BACKENDS: dict[str, TreeSitterLanguage] = {}


def tree_sitter_fallback_resolver(relative_path: str) -> StructuralLanguage | None:
    """Return a per-language :class:`TreeSitterLanguage` for ``relative_path``, or ``None``.

    Registered on the structural registry as the unknown-extension fallback: a
    path whose suffix/basename maps to a shipped tree-sitter grammar gets a
    memoized backend; anything else returns ``None`` so the caller drops to the
    plaintext floor.
    """
    name = _language_name_for_path(relative_path)
    if name is None:
        return None
    backend = _BACKENDS.get(name)
    if backend is None:
        backend = TreeSitterLanguage(name)
        _BACKENDS[name] = backend
    return backend


@dataclass(frozen=True)
class _TreeSitterNode:
    """An addressable tree-sitter node projected to the fields the cursor reads.

    Carries the exact byte span (for :meth:`TreeSitterLanguage.render_node_source`)
    and 0-based row span (for :meth:`TreeSitterLanguage.node_line_range`) plus a
    shared reference to the whole-source bytes it slices from. The native
    tree-sitter ``Node`` is deliberately NOT retained: it is bound to a parse the
    mtime cache may discard, whereas these fields are plain values.

    :ivar kind: the node's tree-sitter type (e.g. ``"rule_set"``).
    :ivar start_byte: inclusive start byte offset into :attr:`source`.
    :ivar end_byte: exclusive end byte offset into :attr:`source`.
    :ivar start_row: 0-based first line of the node.
    :ivar end_row: 0-based last line of the node.
    :ivar source: the whole-file UTF-8 bytes the offsets index into.
    """

    kind: str
    start_byte: int
    end_byte: int
    start_row: int
    end_row: int
    source: bytes


@dataclass
class _TreeSitterTree:
    """A parsed tree plus the exact source it came from.

    :ivar tree: the opaque tree-sitter ``Tree`` handle.
    :ivar source_text: the original source; :meth:`TreeSitterLanguage.serialize`
        returns it verbatim so the round-trip is byte-identical by construction.
    :ivar source_bytes: ``source_text`` UTF-8 encoded -- the slicing basis for
        node byte offsets (tree-sitter byte offsets are UTF-8 byte offsets).
    :ivar language_name: the tree-sitter language this tree was parsed with.
    :ivar has_error: whether the root parsed with any ERROR / MISSING node -- the
        rendered error STATE (spec-v2 §5.8), never an exception.
    """

    tree: Any
    source_text: str
    source_bytes: bytes
    language_name: str
    has_error: bool


class _TreeSitterNameResolver:
    """Read-only name-resolver stub satisfying the :class:`LogicalNameResolver` protocol.

    The tree-sitter rung addresses nodes by walked name path, not by logical
    name, so both resolver methods refuse cleanly with :class:`NameResolutionError`.
    """

    def __init__(self, language_name: str) -> None:
        self._language_name = language_name

    def parse(self, raw: str) -> LogicalName:
        raise NameResolutionError(raw, f"tree-sitter:{self._language_name} addresses nodes by walk name-path, not logical name")

    def resolve(self, name: LogicalName) -> NameResolution:
        raise NameResolutionError(name.raw, f"tree-sitter:{self._language_name} addresses nodes by walk name-path, not logical name")


class TreeSitterLanguage(StructuralLanguage):
    """Read + byte-span-write structural backend over one tree-sitter grammar.

    One instance is bound to one language (the registry memoizes per language via
    :func:`tree_sitter_fallback_resolver`, because the ABC ``parse(source)`` has no
    path to route on). The write-side ABC methods raise :class:`NotImplementedError`:
    structural editing on a tree-sitter-only language is deferred (spec-v2 §5.8);
    byte-span edits go through ``cursor_replace_range``.
    """

    def __init__(self, language_name: str) -> None:
        self._name = language_name
        self._parser: Any = None  # built lazily on first parse
        self._name_resolver = _TreeSitterNameResolver(language_name)

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return f"tree-sitter:{self._name}"

    @property
    def kind_schema(self) -> KindSchema:
        # the read path never validates against this schema (no read-surface call
        # to .kind_schema); a minimal well-formed schema suffices
        return KindSchema(language_key=self.language_key, source_kinds=frozenset({"source"}), kinds={})

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- parse / serialize -------------------------------------------------

    def _get_parser(self) -> Any:
        if self._parser is None:
            self._parser = get_parser(self._name)
        return self._parser

    def parse(self, source: str) -> _TreeSitterTree:
        # the pack binding takes ONE str arg and NEVER raises on malformed input
        # (it produces ERROR nodes); that is surfaced via has_error, not an exception
        tree = self._get_parser().parse(source)
        return _TreeSitterTree(
            tree=tree,
            source_text=source,
            source_bytes=source.encode("utf-8"),
            language_name=self._name,
            has_error=tree.root_node().has_error(),
        )

    def serialize(self, tree: Any) -> str:
        if not isinstance(tree, _TreeSitterTree):
            raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")
        # the original source is retained verbatim -> round-trip is exact
        return tree.source_text

    def render_node_source(self, node: Any) -> str:
        if not isinstance(node, _TreeSitterNode):
            return super().render_node_source(node)
        # exact byte slice (spec-v2 §5.8: source_bytes[start_byte:end_byte].decode())
        return node.source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def node_line_range(self, node: Any) -> tuple[int, int] | None:
        if not isinstance(node, _TreeSitterNode):
            return None
        # 0-based rows; the cursor converts to the 1-based cat -n range at display
        return (node.start_row, node.end_row)

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _TreeSitterTree):
            raise TypeError(f"root_kind expects a _TreeSitterTree, got {type(tree).__name__}")
        return tree.tree.root_node().kind()

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, _TreeSitterNode]]:
        if not isinstance(tree, _TreeSitterTree):
            raise TypeError(f"walk_symbols expects a _TreeSitterTree, got {type(tree).__name__}")
        results: list[tuple[str, KindName, _TreeSitterNode]] = []
        self._walk_named(tree.tree.root_node(), "", 0, tree.source_bytes, results)
        return results

    def _walk_named(
        self,
        node: Any,
        prefix: str,
        depth: int,
        source_bytes: bytes,
        out: list[tuple[str, KindName, _TreeSitterNode]],
    ) -> None:
        """Descend NAMED children, building ``Parent/Child`` name paths.

        Each child's segment is its ``name`` field text when present, else
        ``<kind>#<ordinal>`` (ordinal per parent per kind, so same-kind siblings
        stay distinct). ERROR / MISSING nodes are skipped as symbols -- they never
        break the walk -- and the descent stops at :data:`_WALK_MAX_DEPTH` levels.
        """
        if depth >= _WALK_MAX_DEPTH:
            return
        ordinals: dict[str, int] = {}
        for i in range(node.named_child_count()):
            child = node.named_child(i)
            if child.is_error() or child.is_missing():
                continue
            kind = child.kind()
            segment = self._name_segment(child, kind, ordinals, source_bytes)
            name_path = f"{prefix}/{segment}" if prefix else segment
            out.append((name_path, kind, self._project_node(child, source_bytes)))
            self._walk_named(child, name_path, depth + 1, source_bytes, out)

    @staticmethod
    def _name_segment(child: Any, kind: str, ordinals: dict[str, int], source_bytes: bytes) -> str:
        """Return a stable path segment for ``child``: its ``name`` field, else ``kind#ordinal``."""
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            text = source_bytes[name_node.start_byte() : name_node.end_byte()].decode("utf-8", "replace")
            if text:
                return text
        ordinal = ordinals.get(kind, 0)
        ordinals[kind] = ordinal + 1
        return f"{kind}#{ordinal}"

    @staticmethod
    def _project_node(child: Any, source_bytes: bytes) -> _TreeSitterNode:
        """Project a native tree-sitter node to a plain-value :class:`_TreeSitterNode`."""
        return _TreeSitterNode(
            kind=child.kind(),
            start_byte=child.start_byte(),
            end_byte=child.end_byte(),
            start_row=child.start_position().row,
            end_row=child.end_position().row,
            source=source_bytes,
        )

    # ---- structural editing: DEFERRED (spec-v2 §5.8) ----------------------
    #
    # Arbitrary re-emission on a tree-sitter-only language is out of cut one;
    # byte-span writes go through cursor_replace_range (line-based, never touches
    # the backend), so every write-side ABC method refuses cleanly.

    def _unsupported(self, op: str) -> NotImplementedError:
        return NotImplementedError(
            f"tree-sitter backend ({self._name}) is read + byte-span-write only; "
            f"{op} (structural editing) is unsupported -- use cursor_replace_range",
        )

    def build_declaration(self, kind: KindName, attributes: Mapping[str, Any], children: Iterable[Any]) -> Any:
        raise self._unsupported("build_declaration")

    def insert_child(self, parent: Any, child: Any, anchor: Any | None = None, position: str = "end") -> Any:
        raise self._unsupported("insert_child")

    def remove_child(self, parent: Any, child: Any) -> Any:
        raise self._unsupported("remove_child")

    def compile_pattern(self, pattern_source: str) -> Any:
        raise self._unsupported("compile_pattern")

    def find_matches(self, tree: Any, pattern: Any, scope: Any | None = None) -> Iterable[Any]:
        raise self._unsupported("find_matches")

    def render_replacement(self, replacement_source: str, bindings: Mapping[str, Any]) -> Any:
        raise self._unsupported("render_replacement")

    def apply_replacement(self, tree: Any, match: Any, replacement: Any) -> Any:
        raise self._unsupported("apply_replacement")

    def empty_source(self, source_kind: KindName) -> Any:
        raise self._unsupported("empty_source")


__all__ = ["TreeSitterLanguage", "tree_sitter_fallback_resolver"]
