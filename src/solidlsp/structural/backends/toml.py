"""TOML :class:`StructuralLanguage` backend using tomlkit.

Round-trip strategy (same shape as the yaml backend): the opaque tree
handle carries the original source verbatim, so
``serialize(parse(src)) == src`` for every accepted input. Mutations
rebuild the source by manipulating the tomlkit document (surgically, via
``Container.body`` when ordering matters so comments survive) and then
re-parse the dumped text so the returned handle carries a stable source
under subsequent serializations. The tomlkit round-trip dumper is
idempotent over the toml subset this backend accepts, so re-parsing a
mutation output produces the same text again.

Public entry points:

* :class:`TomlStructuralLanguage` -- the :class:`StructuralLanguage` impl.
* :class:`TomlLogicalNameResolver` -- slash/dotted path -> ``.toml`` file.
* :func:`toml_kind_schema` -- factory for the toml kind vocabulary.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT, Array, InlineTable, SingleKey, Table
from tomlkit.toml_document import TOMLDocument

from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.errors import (
    DeclarationError,
    NameResolutionError,
    ParseError,
    PatternError,
)
from solidlsp.structural.kinds import AttributeSpec, KindName, KindSchema, KindSpec
from solidlsp.structural.names import LogicalName, LogicalNameResolver, NameResolution
from solidlsp.structural.patterns import AstPattern, PatternMatch

# --------------------------------------------------------------------------- #
# Tree handles
# --------------------------------------------------------------------------- #


@dataclass
class _TomlTree:
    """Opaque parse handle for a toml document.

    :ivar source: the source text this tree was parsed from, stored verbatim.
        :meth:`TomlStructuralLanguage.serialize` returns this string directly,
        so the round-trip invariant holds for every source the backend
        accepts (an input that doesn't survive tomlkit round-trip is rejected
        at :meth:`~TomlStructuralLanguage.parse` time).
    :ivar data: the tomlkit :class:`TOMLDocument`. Used for navigation and
        mutation.
    """

    source: str
    data: TOMLDocument


@dataclass
class _TomlDeclaration:
    """An unsealed toml fragment produced by :meth:`build_declaration`.

    Declarations are intermediate values: they are assembled via
    :func:`build_declaration` and then placed into a tree by
    :meth:`insert_child`.

    :ivar kind: the schema kind this declaration represents.
    :ivar data: the tomlkit item / container payload; used when the
        declaration is inserted into a tree.
    :ivar key: for ``pair`` declarations, the decoded mapping key; ``None``
        otherwise.
    """

    kind: KindName
    data: Any
    key: str | None = None


# --------------------------------------------------------------------------- #
# tomlkit engine helpers
# --------------------------------------------------------------------------- #


def _dump(data: TOMLDocument) -> str:
    """Dump ``data`` via tomlkit, returning the string output."""
    return data.as_string()


def _load(source: str) -> TOMLDocument:
    """Load ``source`` via tomlkit."""
    return tomlkit.parse(source)


# --------------------------------------------------------------------------- #
# Kind schema
# --------------------------------------------------------------------------- #


_ATTR_KEY = AttributeSpec(
    name="key",
    type_hint="str",
    required=True,
    description="mapping key; the backend bare-form quotes only if toml syntax requires",
)
_ATTR_SCALAR_VALUE = AttributeSpec(
    name="value",
    type_hint="str | int | float | bool | None",
    required=True,
    description="the python value the scalar represents; bool/int/float/None are rendered natively, str is emitted as a basic string",
)


def toml_kind_schema() -> KindSchema:
    """Return the toml structural kind vocabulary.

    The vocabulary captures the toml value grammar: a ``document`` is the
    top level (itself a mapping); ``table`` is a named or inline mapping;
    ``pair`` is a keyed entry; ``array`` is ``[...]``; ``aot`` is a
    ``[[foo]]`` array of tables; ``scalar`` is a leaf value.
    """
    _MAPPING_CHILDREN = frozenset({"pair", "table", "aot"})
    _VALUE_KINDS = frozenset({"scalar", "array", "table"})

    document = KindSpec(
        name="document",
        description="A toml document's top level; contains pairs, tables, and array-of-tables blocks.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=_MAPPING_CHILDREN,
    )
    table = KindSpec(
        name="table",
        description="A toml table, either a ``[name]`` section or an inline ``{a = 1}`` table.",
        attributes=(),
        allowed_parent_kinds=frozenset({"document", "table", "pair", "array", "aot"}),
        allowed_child_kinds=_MAPPING_CHILDREN,
    )
    pair = KindSpec(
        name="pair",
        description="A key/value entry within a toml mapping.",
        attributes=(_ATTR_KEY,),
        allowed_parent_kinds=frozenset({"document", "table"}),
        allowed_child_kinds=_VALUE_KINDS,
    )
    array = KindSpec(
        name="array",
        description="A toml array ``[...]``; holds scalars, nested arrays, or inline tables.",
        attributes=(),
        allowed_parent_kinds=frozenset({"pair", "array"}),
        allowed_child_kinds=_VALUE_KINDS,
    )
    aot = KindSpec(
        name="aot",
        description="A toml array-of-tables ``[[name]]``; each element is a table.",
        attributes=(),
        allowed_parent_kinds=frozenset({"document", "table"}),
        allowed_child_kinds=frozenset({"table"}),
    )
    scalar = KindSpec(
        name="scalar",
        description="A toml scalar value (string, integer, float, boolean, or null/absent).",
        attributes=(_ATTR_SCALAR_VALUE,),
        allowed_parent_kinds=frozenset({"pair", "array"}),
        allowed_child_kinds=frozenset(),
    )

    return KindSchema(
        language_key="toml",
        source_kinds=frozenset({"document"}),
        kinds={
            "document": document,
            "table": table,
            "pair": pair,
            "array": array,
            "aot": aot,
            "scalar": scalar,
        },
    )


_TOML_KIND_SCHEMA = toml_kind_schema()


# --------------------------------------------------------------------------- #
# Logical name resolution
# --------------------------------------------------------------------------- #


_TOML_NAME_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*")


class TomlLogicalNameResolver(LogicalNameResolver):
    """Maps a slashed or dotted logical name to a ``.toml`` file.

    The resolver is file-level: it identifies which toml file a logical
    name points at. Inside-file navigation is handled by
    :func:`walk_symbols`, not by this resolver.

    :ivar _project_root: project root; resolutions report paths relative to it.
    :ivar _source_roots: ordered content directories to probe.
    :ivar _extensions: file extensions to try, in order (first match wins on
        lookup; first entry is used when creating a new file).
    """

    _DEFAULT_EXTENSIONS: tuple[str, ...] = (".toml",)

    def __init__(
        self,
        project_root: Path,
        source_roots: Sequence[Path] = (),
        extensions: Sequence[str] = (),
    ):
        """:param project_root: directory at whose root paths are reported.
        :param source_roots: directories under which toml files live;
            defaults to ``(project_root,)``.
        :param extensions: file extensions probed in order; defaults to
            :attr:`_DEFAULT_EXTENSIONS`.
        """
        self._project_root = project_root.resolve()
        resolved_roots = tuple(r.resolve() for r in source_roots)
        self._source_roots: tuple[Path, ...] = resolved_roots if resolved_roots else (self._project_root,)
        self._extensions: tuple[str, ...] = tuple(extensions) if extensions else self._DEFAULT_EXTENSIONS

    def parse(self, raw: str) -> LogicalName:
        if not raw:
            raise NameResolutionError(raw, "empty logical name")
        parts = tuple(re.split(r"[./]", raw))
        for part in parts:
            if not part or not _TOML_NAME_PART.fullmatch(part):
                raise NameResolutionError(raw, f"invalid toml name part: {part!r}")
        return LogicalName(parts=parts, raw=raw)

    def resolve(self, name: LogicalName) -> NameResolution:
        rel = Path(*name.parts)
        for root in self._source_roots:
            for ext in self._extensions:
                candidate = root / rel.with_suffix(ext)
                if candidate.is_file():
                    return self._resolution_for(candidate, exists=True)
        synthetic = self._source_roots[0] / rel.with_suffix(self._extensions[0])
        return self._resolution_for(synthetic, exists=False)

    def _resolution_for(self, absolute: Path, exists: bool) -> NameResolution:
        try:
            relative = absolute.relative_to(self._project_root)
        except ValueError as err:
            raise NameResolutionError(
                str(absolute),
                f"resolved path {absolute} escapes project root {self._project_root}",
            ) from err
        return NameResolution(relative_path=str(relative), source_kind="document", exists=exists)


# --------------------------------------------------------------------------- #
# Symbol walking
# --------------------------------------------------------------------------- #


def _is_mapping_like(node: Any) -> bool:
    """True if ``node`` behaves like a toml mapping (document, table, inline table)."""
    return isinstance(node, TOMLDocument | Container | Table | InlineTable)


def _value_kind(node: Any) -> KindName:
    """Return the schema kind name for a toml value node.

    Documents and tables (including inline) report ``table``. AoT reports
    ``aot``. Arrays report ``array``. Anything else is a scalar.
    """
    if isinstance(node, TOMLDocument):
        return "table"
    if isinstance(node, Container | Table | InlineTable):
        return "table"
    if isinstance(node, AoT):
        return "aot"
    if isinstance(node, Array):
        return "array"
    return "scalar"


def _stringify_key(key: Any) -> str:
    """Render a mapping key as its string form for name-path use."""
    if isinstance(key, SingleKey):
        return str(key.key)
    return str(key)


def _walk(node: Any, prefix: str) -> Iterable[tuple[str, KindName, Any]]:
    """Yield addressable symbols under ``node`` with name paths built on ``prefix``.

    The walk yields every mapping pair, every array element, and every AoT
    element. Compound values (tables, arrays, aots) are walked recursively.
    Scalar values directly under a pair are not yielded separately -- the
    pair itself is the addressable symbol.
    """
    if _is_mapping_like(node):
        for key in list(node.keys()):
            rendered = _stringify_key(key)
            pair_path = f"{prefix}/{rendered}" if prefix else rendered
            value = node[key]
            yield pair_path, "pair", (node, key)
            yield from _walk(value, pair_path)
        return

    if isinstance(node, AoT):
        for index, item in enumerate(node):
            segment = f"[{index}]"
            item_path = f"{prefix}/{segment}" if prefix else segment
            yield item_path, "table", item
            yield from _walk(item, item_path)
        return

    if isinstance(node, Array):
        for index, item in enumerate(node):
            segment = f"[{index}]"
            item_path = f"{prefix}/{segment}" if prefix else segment
            yield item_path, _value_kind(item), item
            yield from _walk(item, item_path)
        return

    # scalars contribute no addressable symbols of their own
    return


def walk_symbols(tree: _TomlTree) -> Iterable[tuple[str, KindName, Any]]:
    """Yield ``(name_path, kind, node)`` for every addressable toml symbol in ``tree``.

    Mapping pairs, array items, and AoT entries are addressable. Name paths
    are slash-separated; array and AoT items use ``[N]`` segments. The root
    document carries no name path and is not yielded; only pairs and
    elements below it are.

    :raises TypeError: if ``tree`` is not a :class:`_TomlTree`.
    """
    if not isinstance(tree, _TomlTree):
        raise TypeError(f"walk_symbols expects a _TomlTree, got {type(tree).__name__}")
    return list(_walk(tree.data, ""))


# --------------------------------------------------------------------------- #
# Pattern matching
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _TomlPattern:
    """A compiled toml pattern: the pattern source plus its loaded tree."""

    source: str
    data: TOMLDocument


@dataclass(frozen=True)
class _TomlReplacement:
    """A rendered replacement fragment ready for :meth:`apply_replacement`."""

    data: Any


_CAPTURE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _capture_kind(literal: Any) -> tuple[str, str] | None:
    """Classify a scalar literal as a capture sigil.

    Captures are embedded inside toml strings:

    * ``"$_"`` -- a wildcard that matches any value.
    * ``"$name"`` -- a capture that matches any value and binds it under
      ``name``.

    Returns ``(kind, name)`` where ``kind`` is ``"wildcard"`` or
    ``"capture"`` and ``name`` is the capture identifier (``"_"`` for
    wildcards). Returns ``None`` when the literal is not a sigil.
    """
    if not isinstance(literal, str) or not literal.startswith("$"):
        return None
    rest = literal[1:]
    if rest == "_":
        return ("wildcard", "_")
    if _CAPTURE_NAME.fullmatch(rest):
        return ("capture", rest)
    return None


def _iter_value_nodes(node: Any) -> Iterable[Any]:
    """Yield ``node`` and every nested toml value node in document order."""
    yield node
    if _is_mapping_like(node):
        for key in list(node.keys()):
            yield from _iter_value_nodes(node[key])
        return
    if isinstance(node, AoT | Array):
        for item in node:
            yield from _iter_value_nodes(item)


def _match_value(pattern: Any, target: Any, bindings: dict[str, Any]) -> bool:
    """Match ``pattern`` against ``target``, recording captures into ``bindings``.

    Matching rules:

    * A string scalar ``"$_"`` matches any value without binding.
    * A string scalar ``"$name"`` matches any value and binds it under
      ``name``. Re-binding to a different value fails the match.
    * Mapping-like values (document, table, inline table) match when their
      key sets are equal (order-independent) and values match recursively.
    * AoT and Array match when lengths are equal and items match in order;
      AoT-vs-Array cross-kind mismatches are rejected.
    * Other scalars match when equal under Python's ``==``.
    """
    if isinstance(pattern, str):
        sigil = _capture_kind(pattern)
        if sigil is not None:
            kind, name = sigil
            if kind == "wildcard":
                return True
            existing = bindings.get(name)
            if existing is None:
                bindings[name] = target
                return True
            return existing == target

    if _is_mapping_like(pattern):
        if not _is_mapping_like(target):
            return False
        pkeys = {_stringify_key(k) for k in pattern.keys()}
        tkeys = {_stringify_key(k) for k in target.keys()}
        if pkeys != tkeys:
            return False
        # iterate pattern keys, look up same key in target by stringified form
        target_by_str = {_stringify_key(k): k for k in target.keys()}
        for pk in pattern.keys():
            tk = target_by_str[_stringify_key(pk)]
            if not _match_value(pattern[pk], target[tk], bindings):
                return False
        return True

    if isinstance(pattern, AoT):
        if not isinstance(target, AoT):
            return False
        if len(pattern) != len(target):
            return False
        for p_item, t_item in zip(pattern, target, strict=False):
            if not _match_value(p_item, t_item, bindings):
                return False
        return True

    if isinstance(pattern, Array):
        if not isinstance(target, Array):
            return False
        if len(pattern) != len(target):
            return False
        for p_item, t_item in zip(pattern, target, strict=False):
            if not _match_value(p_item, t_item, bindings):
                return False
        return True

    return pattern == target


def _render_capture_resolution(node: Any, bindings: Mapping[str, Any]) -> Any:
    """Substitute capture references in a replacement tree with their bindings.

    Walks the loaded replacement data and, wherever a scalar ``"$name"`` is
    found, replaces it with ``bindings[name]``. Mappings, arrays, and AoTs
    are rebuilt with substituted children so the original replacement tree
    is not mutated.
    """
    if isinstance(node, str):
        sigil = _capture_kind(node)
        if sigil is not None:
            kind, name = sigil
            if kind == "wildcard":
                raise PatternError("parse", "replacement may not contain wildcard $_")
            if name not in bindings:
                raise PatternError("parse", f"replacement references unbound capture ${name}")
            return bindings[name]
        return node

    if _is_mapping_like(node):
        # rebuild into a fresh tomlkit table so the original isn't mutated
        rebuilt = tomlkit.table() if not isinstance(node, InlineTable) else tomlkit.inline_table()
        for key in list(node.keys()):
            rebuilt[_stringify_key(key)] = _render_capture_resolution(node[key], bindings)
        return rebuilt

    if isinstance(node, AoT):
        aot = tomlkit.aot()
        for item in node:
            rebuilt_item = _render_capture_resolution(item, bindings)
            if not isinstance(rebuilt_item, Table):
                # coerce a substituted mapping into a table if needed
                coerced = tomlkit.table()
                if _is_mapping_like(rebuilt_item):
                    for k in list(rebuilt_item.keys()):
                        coerced[_stringify_key(k)] = rebuilt_item[k]
                    rebuilt_item = coerced
            aot.append(rebuilt_item)
        return aot

    if isinstance(node, Array):
        arr = tomlkit.array()
        for item in node:
            arr.append(_render_capture_resolution(item, bindings))
        return arr

    return node


def _replace_in_tree(root: Any, target: Any, replacement: Any) -> tuple[Any, bool]:
    """Return ``root`` with ``target`` replaced by ``replacement`` (in-place).

    The replacement is located by object identity (``is``). Returns a
    ``(new_root, found)`` pair so callers can detect failure without
    raising from deep inside the recursion. ``root`` is mutated in place
    when ``target`` is nested under it.
    """
    if root is target:
        return replacement, True

    if _is_mapping_like(root):
        for key in list(root.keys()):
            new_value, found = _replace_in_tree(root[key], target, replacement)
            if found:
                root[key] = new_value
                return root, True
        return root, False

    if isinstance(root, AoT | Array):
        for index in range(len(root)):
            new_item, found = _replace_in_tree(root[index], target, replacement)
            if found:
                root[index] = new_item
                return root, True
        return root, False

    return root, False


# --------------------------------------------------------------------------- #
# Attribute helpers
# --------------------------------------------------------------------------- #


def _require_str(attrs: Mapping[str, Any], name: str, kind: KindName) -> str:
    if name not in attrs or attrs[name] is None:
        raise DeclarationError(kind, f"attribute {name!r} is required")
    value = attrs[name]
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be str, got {type(value).__name__}")
    return value


_SCALAR_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


def _validate_scalar_value(value: Any, kind: KindName) -> None:
    if not isinstance(value, _SCALAR_TYPES):
        raise DeclarationError(kind, f"scalar value must be str|int|float|bool|None, got {type(value).__name__}")


# --------------------------------------------------------------------------- #
# TomlStructuralLanguage
# --------------------------------------------------------------------------- #


class TomlStructuralLanguage(StructuralLanguage):
    """Structural backend for toml documents.

    Parses with :mod:`tomlkit`. The parse handle stores the source verbatim,
    so ``serialize(parse(s)) == s`` is satisfied by returning the stored
    source. Any source that doesn't round-trip through tomlkit is rejected
    at parse time so the backend only accepts inputs for which the
    invariant holds.

    Mutations modify a deep copy of the tomlkit document, re-dump to toml,
    and re-parse the dumped text. Because the tomlkit round-trip dumper is
    idempotent on its own output, the new handle satisfies the invariant
    again.
    """

    def __init__(self, name_resolver: LogicalNameResolver | None = None):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
        Defaults to a :class:`TomlLogicalNameResolver` rooted at the current
        working directory.
        """
        self._name_resolver = name_resolver or TomlLogicalNameResolver(Path.cwd())

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "toml"

    @property
    def kind_schema(self) -> KindSchema:
        return _TOML_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- parse / serialize -------------------------------------------------

    def parse(self, source: str) -> _TomlTree:
        try:
            data = _load(source)
        except TOMLKitError as err:
            raise ParseError(language_key="toml", source_preview=source[:240], detail=str(err)) from err

        dumped = _dump(data)
        if dumped != source:
            raise ParseError(
                language_key="toml",
                source_preview=source[:240],
                detail=(f"source does not round-trip through tomlkit; re-dump differs. expected={source!r} got={dumped!r}"),
            )
        return _TomlTree(source=source, data=data)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _TomlTree):
            return tree.source
        if isinstance(tree, _TomlDeclaration):
            if hasattr(tree.data, "as_string"):
                return tree.data.as_string()
            return str(tree.data)
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _TomlTree):
            raise TypeError(f"root_kind expects a _TomlTree, got {type(tree).__name__}")
        return "document"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        return walk_symbols(tree)

    # ---- declaration -------------------------------------------------------

    def build_declaration(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
        children: Iterable[Any],
    ) -> _TomlDeclaration:
        self.kind_schema.get(kind)
        children_list = list(children)

        if kind == "document":
            raise DeclarationError(kind, "construct document via empty_source() plus insert_child()")

        if kind == "scalar":
            if children_list:
                raise DeclarationError(kind, "scalar takes no children")
            if "value" not in attributes:
                raise DeclarationError(kind, "attribute 'value' is required")
            value = attributes["value"]
            _validate_scalar_value(value, kind)
            if value is None:
                # toml has no native null; reject explicitly so the declaration
                # isn't silently rendered as empty / default
                raise DeclarationError(kind, "toml has no null scalar; use an absent pair instead")
            item = tomlkit.item(value)
            return _TomlDeclaration(kind=kind, data=item)

        if kind == "pair":
            key = _require_str(attributes, "key", kind)
            if len(children_list) != 1:
                raise DeclarationError(kind, f"pair requires exactly one value child, got {len(children_list)}")
            value_child = children_list[0]
            if not isinstance(value_child, _TomlDeclaration):
                raise DeclarationError(kind, f"pair child must be a _TomlDeclaration from this backend; got {type(value_child).__name__}")
            return _TomlDeclaration(kind=kind, data=value_child.data, key=key)

        if kind == "table":
            table_data: Table = tomlkit.table()
            for idx, child in enumerate(children_list):
                if not isinstance(child, _TomlDeclaration) or child.kind != "pair":
                    raise DeclarationError(kind, f"table child {idx} must be a pair declaration, got {type(child).__name__}")
                if child.key is None:
                    raise DeclarationError(kind, f"table child {idx} is missing its key")
                if child.key in table_data:
                    raise DeclarationError(kind, f"duplicate key {child.key!r} in table")
                table_data[child.key] = child.data
            return _TomlDeclaration(kind=kind, data=table_data)

        if kind == "array":
            array_data: Array = tomlkit.array()
            for idx, child in enumerate(children_list):
                if not isinstance(child, _TomlDeclaration):
                    raise DeclarationError(kind, f"array child {idx} must be a _TomlDeclaration, got {type(child).__name__}")
                if child.kind == "pair":
                    raise DeclarationError(kind, f"array child {idx} must be a value, not a pair")
                if child.kind == "aot":
                    raise DeclarationError(kind, f"array child {idx} cannot be an aot; aots live at mapping level")
                array_data.append(child.data)
            return _TomlDeclaration(kind=kind, data=array_data)

        if kind == "aot":
            aot_data: AoT = tomlkit.aot()
            for idx, child in enumerate(children_list):
                if not isinstance(child, _TomlDeclaration) or child.kind != "table":
                    got = child.kind if isinstance(child, _TomlDeclaration) else type(child).__name__
                    raise DeclarationError(kind, f"aot child {idx} must be a table declaration, got {got}")
                aot_data.append(child.data)
            return _TomlDeclaration(kind=kind, data=aot_data)

        raise DeclarationError(kind, f"build_declaration not supported for kind {kind!r}")

    # ---- insert / remove ---------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _TomlTree:
        if not isinstance(parent, _TomlTree):
            raise TypeError(f"parent must be _TomlTree, got {type(parent).__name__}")
        if not isinstance(child, _TomlDeclaration):
            raise TypeError(f"child must be a _TomlDeclaration, got {type(child).__name__}")
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")

        root = parent.data
        if child.kind != "pair":
            raise TypeError(
                f"inserting into a toml mapping requires a pair declaration, got kind {child.kind!r}",
            )
        if child.key is None:
            raise DeclarationError(child.kind, "pair declaration is missing its key")
        if child.key in root:
            raise DeclarationError(child.kind, f"duplicate key {child.key!r} in mapping")
        new_root = _mapping_with_inserted(root, child.key, child.data, anchor, position)
        return self.parse(_dump(new_root))

    def remove_child(self, parent: Any, child: Any) -> _TomlTree:
        if not isinstance(parent, _TomlTree):
            raise TypeError(f"parent must be _TomlTree, got {type(parent).__name__}")
        root = parent.data
        new_root = _mapping_without(root, child)
        return self.parse(_dump(new_root))

    # ---- pattern matching & rewriting --------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError("parse", "pattern source is empty")
        try:
            data = _load(pattern_source)
        except TOMLKitError as err:
            raise PatternError("parse", f"pattern source is not valid toml: {err}") from err
        if len(data.keys()) == 0:
            raise PatternError("parse", "pattern source has no entries; cannot match anything")
        return _TomlPattern(source=pattern_source, data=data)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _TomlTree):
            raise TypeError(f"tree must be a _TomlTree, got {type(tree).__name__}")
        if not isinstance(pattern, _TomlPattern):
            raise TypeError(f"pattern must come from this backend's compile_pattern, got {type(pattern).__name__}")
        root: Any = tree.data
        if scope is not None:
            root = scope
        matches: list[PatternMatch] = []
        for candidate in _iter_value_nodes(root):
            bindings: dict[str, Any] = {}
            if _match_value(pattern.data, candidate, bindings):
                matches.append(PatternMatch(node=candidate, bindings=dict(bindings), symbol_path=None))
        return matches

    def render_replacement(
        self,
        replacement_source: str,
        bindings: Mapping[str, Any],
    ) -> _TomlReplacement:
        if not replacement_source.strip():
            raise PatternError("parse", "replacement source is empty")
        try:
            data = _load(replacement_source)
        except TOMLKitError as err:
            raise PatternError("parse", f"replacement source is not valid toml: {err}") from err
        if len(data.keys()) == 0:
            raise PatternError("parse", "replacement evaluates to empty; must have at least one entry")
        resolved = _render_capture_resolution(data, dict(bindings))
        return _TomlReplacement(data=resolved)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _TomlTree:
        if not isinstance(tree, _TomlTree):
            raise TypeError(f"tree must be a _TomlTree, got {type(tree).__name__}")
        if not isinstance(replacement, _TomlReplacement):
            raise TypeError(f"replacement must come from this backend's render_replacement, got {type(replacement).__name__}")
        cloned = copy.deepcopy(tree.data)
        target = _locate_by_path(tree.data, cloned, match.node)
        if target is None:
            raise ValueError("match.node was not found in tree")
        new_root, found = _replace_in_tree(cloned, target, replacement.data)
        if not found:
            raise ValueError("match.node was not found in tree")
        if new_root is target:
            # replacement at root: wrap into a TOMLDocument by rebuilding
            replaced_doc = tomlkit.document()
            if _is_mapping_like(replacement.data):
                for key in list(replacement.data.keys()):
                    replaced_doc[_stringify_key(key)] = replacement.data[key]
                return self.parse(_dump(replaced_doc))
            raise ValueError("cannot replace the document root with a non-mapping value")
        return self.parse(_dump(new_root))

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _TomlTree:
        if source_kind != "document":
            raise DeclarationError(source_kind, f"toml has no source kind {source_kind!r}")
        return _TomlTree(source="", data=tomlkit.document())

    def container_insert_member(
        self,
        tree: Any,
        anchor_or_container_path: str,
        source: str,
        position: str = "end",
    ) -> _TomlTree:
        # ABC-level shape and arg validation
        if not isinstance(tree, _TomlTree):
            raise TypeError(f"tree must be _TomlTree, got {type(tree).__name__}")
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")

        # start/end target the container directly; before/after target a sibling
        if position in {"start", "end"}:
            container_path = anchor_or_container_path
            anchor_segment: str | None = None
        else:
            container_path, anchor_segment = _split_member_path_toml(anchor_or_container_path)

        # deepcopy once, then walk + mutate in place so surrounding comments stay anchored
        cloned_root = copy.deepcopy(tree.data)
        container = _resolve_container_toml(cloned_root, container_path)

        # dispatch on container kind
        if _is_mapping_like(container):
            key_str, value = _parse_toml_member(source)
            existing_keys = [_stringify_key(k) for k in container.keys()]
            if key_str in existing_keys:
                raise DeclarationError("pair", f"duplicate key {key_str!r} in mapping")
            _mapping_insert_in_place(container, key_str, value, anchor_segment, position)
        elif isinstance(container, AoT | Array):
            anchor_index: int | None = None
            if anchor_segment is not None:
                anchor_index = _parse_sequence_index_segment(anchor_segment)
            value = _parse_toml_value(source)
            _sequence_insert_in_place(container, value, anchor_index, position)
        else:
            raise ValueError(
                f"container at {container_path!r} is not a mapping or sequence",
            )

        return self.parse(_dump(cloned_root))

    def container_remove_member(self, tree: Any, member_path: str) -> _TomlTree:
        if not isinstance(tree, _TomlTree):
            raise TypeError(f"tree must be _TomlTree, got {type(tree).__name__}")

        parent_path, last_segment = _split_member_path_toml(member_path)
        cloned_root = copy.deepcopy(tree.data)
        container = _resolve_container_toml(cloned_root, parent_path)

        if _is_mapping_like(container):
            _mapping_remove_in_place(container, last_segment)
        elif isinstance(container, AoT | Array):
            idx = _parse_sequence_index_segment(last_segment)
            _sequence_remove_in_place(container, idx)
        else:
            raise ValueError(
                f"container at {parent_path!r} is not a mapping or sequence",
            )

        return self.parse(_dump(cloned_root))

    def container_replace_member(self, tree: Any, member_path: str, source: str) -> _TomlTree:
        if not isinstance(tree, _TomlTree):
            raise TypeError(f"tree must be _TomlTree, got {type(tree).__name__}")

        parent_path, last_segment = _split_member_path_toml(member_path)
        cloned_root = copy.deepcopy(tree.data)
        container = _resolve_container_toml(cloned_root, parent_path)
        new_value = _parse_toml_value(source)

        if _is_mapping_like(container):
            _mapping_replace_value_in_place(container, last_segment, new_value)
        elif isinstance(container, AoT | Array):
            idx = _parse_sequence_index_segment(last_segment)
            _sequence_replace_in_place(container, idx, new_value)
        else:
            raise ValueError(
                f"container at {parent_path!r} is not a mapping or sequence",
            )

        return self.parse(_dump(cloned_root))


# --------------------------------------------------------------------------- #
# Mutation helpers
# --------------------------------------------------------------------------- #


def _body_of(container_or_table: Any) -> list:
    """Return the underlying ``body`` list for a TOMLDocument/Container/Table.

    TOMLDocument and Container expose ``.body`` directly; Table wraps a
    Container and exposes it via ``.value``. Inline tables also wrap a
    Container behind ``.value``. This helper unifies access.
    """
    if isinstance(container_or_table, TOMLDocument | Container):
        return container_or_table.body
    if isinstance(container_or_table, Table | InlineTable):
        return container_or_table.value.body
    raise TypeError(f"no body on {type(container_or_table).__name__}")


def _keyed_body_index(body: list, key: str) -> int | None:
    """Return the index in ``body`` at which ``key`` lives, or ``None``."""
    for i, (k, _v) in enumerate(body):
        if k is not None and _stringify_key(k) == key:
            return i
    return None


def _end_insertion_index(body: list) -> int:
    """Return the index at which an ``end``-positioned pair should be inserted.

    TOML syntax puts scalar/array pairs before any ``[table]`` or ``[[aot]]``
    header at a given mapping level; once a header appears, subsequent entries
    belong to that table, not the enclosing mapping. A naive ``body.append``
    therefore drops the new pair *inside* the last container. This helper
    finds the position right after the last non-container keyed entry -- or,
    when the mapping has no containers, the very end of the body.
    """
    first_container_idx: int | None = None
    last_pair_idx = -1
    for i, (k, v) in enumerate(body):
        if k is None:
            continue
        if isinstance(v, Table | AoT):
            if first_container_idx is None:
                first_container_idx = i
            continue
        last_pair_idx = i
    if first_container_idx is None:
        return len(body)
    return last_pair_idx + 1


def _mapping_with_inserted(
    mapping: Any,
    key: str,
    value: Any,
    anchor: Any,
    position: str,
) -> Any:
    """Return a deepcopy of ``mapping`` with ``(key, value)`` inserted.

    Body manipulation is used so that surrounding comments stay anchored in
    place relative to their original neighbors rather than being re-emitted
    by tomlkit at the end of the container.
    """
    clone = copy.deepcopy(mapping)
    body = _body_of(clone)
    cloned_value = copy.deepcopy(value)
    new_key = SingleKey(key)
    new_pair = (new_key, cloned_value)

    if position == "start":
        body.insert(0, new_pair)
        return clone
    if position == "end":
        # insert after the last non-container keyed entry so the new pair
        # doesn't get pulled into the scope of a trailing [table] / [[aot]]
        body.insert(_end_insertion_index(body), new_pair)
        return clone

    # before/after: anchor is (mapping_node, anchor_key)
    anchor_key = _mapping_anchor_key(mapping, anchor)
    if anchor_key is None:
        raise ValueError("anchor not found in mapping")
    idx = _keyed_body_index(body, anchor_key)
    if idx is None:
        raise ValueError("anchor not found in mapping body")
    if position == "before":
        body.insert(idx, new_pair)
    else:  # after
        body.insert(idx + 1, new_pair)
    return clone


def _mapping_anchor_key(mapping: Any, anchor: Any) -> str | None:
    """Return the stringified mapping key an anchor identifies.

    Anchors in the toml backend come from :func:`walk_symbols`, which yields
    pair tuples ``(parent, key)`` for mapping entries. This helper accepts
    either that tuple form or a raw key already present in ``mapping``.
    """
    if isinstance(anchor, tuple) and len(anchor) == 2:
        parent, key = anchor
        key_str = _stringify_key(key)
        if parent is mapping and key_str in [_stringify_key(k) for k in mapping.keys()]:
            return key_str
        if key_str in [_stringify_key(k) for k in mapping.keys()]:
            return key_str
        return None
    if isinstance(anchor, str) and anchor in [_stringify_key(k) for k in mapping.keys()]:
        return anchor
    return None


def _mapping_without(mapping: Any, child: Any) -> Any:
    """Return a deepcopy of ``mapping`` with the entry identified by ``child`` removed."""
    clone = copy.deepcopy(mapping)
    key = _mapping_anchor_key(mapping, child)
    if key is None:
        raise ValueError("child not found in mapping")
    del clone[key]
    return clone


def _locate_by_path(original: Any, cloned: Any, target: Any) -> Any:
    """Given a target node in ``original``, return the matching node in ``cloned``.

    ``original`` and ``cloned`` are expected to have identical structure
    (``cloned`` is a deepcopy of ``original``). The function walks both in
    lockstep until it finds ``target`` by identity in ``original``, then
    returns the corresponding node in ``cloned``.
    """
    if original is target:
        return cloned
    if _is_mapping_like(original):
        for key in list(original.keys()):
            if key not in cloned:
                continue
            found = _locate_by_path(original[key], cloned[key], target)
            if found is not None:
                return found
        return None
    if isinstance(original, AoT | Array):
        for i in range(len(original)):
            if i >= len(cloned):
                continue
            found = _locate_by_path(original[i], cloned[i], target)
            if found is not None:
                return found
        return None
    return None


# -----------------------------------------------------------------------------
# Path-based container-member helpers
#
# these power the L3 container_insert_member / container_remove_member /
# container_replace_member ABC methods on TomlStructuralLanguage. the edit
# strategy is: deepcopy the root once, walk the slash-separated name-path to
# the target container inside the clone, then mutate that container in place
# (so surrounding trivia / comments stay anchored in place relative to their
# original neighbors). the re-dumped clone is then re-parsed by the backend,
# giving a fresh _TomlTree with a round-tripped source.
# -----------------------------------------------------------------------------


def _split_member_path_toml(member_path: str) -> tuple[str, str]:
    """Split a member path on its last ``/``.

    ``"foo/bar/baz"`` -> ``("foo/bar", "baz")``. A path with no ``/`` is
    returned as ``("", member_path)`` -- the member is a direct child of
    the document root.
    """
    slash_idx = member_path.rfind("/")
    if slash_idx < 0:
        return "", member_path
    return member_path[:slash_idx], member_path[slash_idx + 1 :]


def _parse_sequence_index_segment(segment: str) -> int:
    """Parse a ``[N]`` bracket segment into its integer index.

    :raises ValueError: if ``segment`` is not of the form ``[N]`` or ``N``
        is not a valid integer.
    """
    if not (segment.startswith("[") and segment.endswith("]")):
        raise ValueError(f"sequence segment must be [N], got {segment!r}")
    try:
        return int(segment[1:-1])
    except ValueError as err:
        raise ValueError(f"invalid sequence index in segment {segment!r}") from err


def _resolve_node_by_path_toml(root: Any, path: str) -> Any:
    """Walk ``root`` along ``path`` and return the terminal node.

    Mapping-key segments descend into the mapping's VALUE for that key
    (matching what :func:`walk_symbols` exposes for a pair entry's
    name-path). Bracketed segments are array / AoT indices.

    :raises ValueError: on empty path, type mismatches, or unresolvable
        segments.
    """
    if not path:
        raise ValueError("empty path does not resolve to a node")
    node: Any = root
    for segment in path.split("/"):
        if segment.startswith("[") and segment.endswith("]"):
            # sequence-index descent
            if not isinstance(node, AoT | Array):
                raise ValueError(
                    f"segment {segment!r} expects AoT/Array, got {type(node).__name__}",
                )
            idx = _parse_sequence_index_segment(segment)
            if idx < 0 or idx >= len(node):
                raise ValueError(
                    f"sequence index {idx} out of range for segment {segment!r}",
                )
            node = node[idx]
        else:
            # mapping-key descent
            if not _is_mapping_like(node):
                raise ValueError(
                    f"segment {segment!r} expects a mapping, got {type(node).__name__}",
                )
            match_key = None
            for k in node.keys():
                if _stringify_key(k) == segment:
                    match_key = k
                    break
            if match_key is None:
                raise ValueError(f"mapping has no key {segment!r}")
            node = node[match_key]
    return node


def _resolve_container_toml(root: Any, container_path: str) -> Any:
    """Resolve ``container_path`` against ``root``, requiring a container node.

    Empty path returns ``root`` itself. Any non-empty path must land on a
    mapping-like node (:class:`TOMLDocument` / :class:`Container` /
    :class:`Table` / :class:`InlineTable`) or on a sequence-like node
    (:class:`AoT` / :class:`Array`).
    """
    if not container_path:
        return root
    node = _resolve_node_by_path_toml(root, container_path)
    if _is_mapping_like(node) or isinstance(node, AoT | Array):
        return node
    raise ValueError(
        f"path {container_path!r} does not resolve to a TOML container",
    )


def _parse_toml_member(source: str) -> tuple[str, Any]:
    """Parse a mapping-member fragment like ``key = value`` and return ``(key, value)``.

    Extra leading whitespace / comments in ``source`` are tolerated; the
    first keyed body entry is returned.
    """
    try:
        doc = _load(source)
    except TOMLKitError as err:
        raise ParseError(
            language_key="toml",
            source_preview=source[:240],
            detail=str(err),
        ) from err
    for key, value in doc.body:
        if key is not None:
            return _stringify_key(key), value
    raise ParseError(
        language_key="toml",
        source_preview=source[:240],
        detail="member source has no key/value pair",
    )


def _parse_toml_value(source: str) -> Any:
    """Parse a bare value expression into a tomlkit value node.

    tomlkit has no public "parse value" entry point, so ``source`` is
    wrapped as the RHS of a throwaway assignment and the parsed value is
    extracted.
    """
    wrapped = f"_structural_value = {source}"
    try:
        doc = _load(wrapped)
    except TOMLKitError as err:
        raise ParseError(
            language_key="toml",
            source_preview=source[:240],
            detail=str(err),
        ) from err
    for key, value in doc.body:
        if key is not None:
            return value
    raise ParseError(
        language_key="toml",
        source_preview=source[:240],
        detail="value source did not yield a value",
    )


def _mapping_insert_in_place(
    mapping: Any,
    key_str: str,
    value: Any,
    anchor_key: str | None,
    position: str,
) -> None:
    """Splice a ``(key, value)`` pair into ``mapping``'s body list in place.

    Direct body-list manipulation keeps surrounding trivia / comments
    anchored in place rather than being re-emitted by tomlkit at the end
    of the container. For block-level mapping contexts (the common case
    for ``[table]`` and root-level documents) the value must carry a
    trailing ``\\n`` in its trivia; otherwise the re-dumped pair runs
    into its successor on the same line and becomes malformed.
    """
    body = _body_of(mapping)
    _ensure_block_trailing_newline(value)
    new_pair = (SingleKey(key_str), value)

    if position == "start":
        body.insert(0, new_pair)
        return
    if position == "end":
        # insert right after the last non-container keyed entry so the new
        # pair doesn't get swallowed by a trailing [table] / [[aot]] scope
        body.insert(_end_insertion_index(body), new_pair)
        return
    if anchor_key is None:
        raise ValueError(f"position {position!r} requires an anchor")
    idx = _keyed_body_index(body, anchor_key)
    if idx is None:
        raise ValueError(f"anchor key {anchor_key!r} not found in mapping")
    if position == "before":
        body.insert(idx, new_pair)
    else:  # after
        body.insert(idx + 1, new_pair)


def _ensure_block_trailing_newline(value: Any) -> None:
    """Ensure ``value``'s trivia ends with ``\\n`` so a block-level pair dumps cleanly.

    tomlkit items parsed from ``key = 42`` (without a trailing newline)
    come back with empty trivia. When such an item is spliced into a
    block-level body list, the dumped output has the next pair running
    onto the same line. Setting the trailing newline restores the
    expected line-per-pair layout.
    """
    if hasattr(value, "trivia") and not value.trivia.trail.endswith("\n"):
        value.trivia.trail = value.trivia.trail + "\n"


def _mapping_remove_in_place(mapping: Any, key_str: str) -> None:
    """Delete the entry keyed by ``key_str`` from ``mapping``."""
    target_key = None
    for k in mapping.keys():
        if _stringify_key(k) == key_str:
            target_key = k
            break
    if target_key is None:
        raise ValueError(f"key {key_str!r} not found in mapping")
    del mapping[target_key]


def _mapping_replace_value_in_place(mapping: Any, key_str: str, new_value: Any) -> None:
    """Replace the value of the entry keyed by ``key_str``, preserving key identity and position.

    The old value's trailing trivia (typically a ``\\n`` for block-level
    pairs) is copied onto the new value, so replacing a pair inside a
    ``[table]`` does not collapse the following pair onto the same line.
    """
    body = _body_of(mapping)
    for i, (k, old_v) in enumerate(body):
        if k is not None and _stringify_key(k) == key_str:
            if hasattr(old_v, "trivia") and hasattr(new_value, "trivia"):
                new_value.trivia.trail = old_v.trivia.trail
            body[i] = (k, new_value)
            return
    raise ValueError(f"key {key_str!r} not found in mapping")


def _sequence_insert_in_place(
    sequence: Any,
    value: Any,
    anchor_index: int | None,
    position: str,
) -> None:
    """Insert ``value`` into an :class:`AoT` / :class:`Array` at ``position`` in place."""
    if position == "start":
        sequence.insert(0, value)
        return
    if position == "end":
        sequence.append(value)
        return
    if anchor_index is None:
        raise ValueError(f"position {position!r} requires an anchor")
    if anchor_index < 0 or anchor_index >= len(sequence):
        raise ValueError(f"anchor index {anchor_index} out of range")
    if position == "before":
        sequence.insert(anchor_index, value)
    else:  # after
        sequence.insert(anchor_index + 1, value)


def _sequence_remove_in_place(sequence: Any, index: int) -> None:
    """Remove the item at ``index`` from ``sequence``."""
    if index < 0 or index >= len(sequence):
        raise ValueError(f"sequence index {index} out of range")
    del sequence[index]


def _sequence_replace_in_place(sequence: Any, index: int, new_value: Any) -> None:
    """Replace the item at ``index`` in ``sequence`` with ``new_value``."""
    if index < 0 or index >= len(sequence):
        raise ValueError(f"sequence index {index} out of range")
    sequence[index] = new_value


__all__ = [
    "TomlLogicalNameResolver",
    "TomlStructuralLanguage",
    "toml_kind_schema",
    "walk_symbols",
]
