"""YAML :class:`StructuralLanguage` backend using ruamel.yaml.

Round-trip strategy (same shape as the markdown backend): the opaque tree
handle carries the original source verbatim, so
``serialize(parse(src)) == src`` for every accepted input. Mutations rebuild
the source by dumping the updated ruamel tree with :class:`ruamel.yaml.YAML`
in round-trip mode, then re-parse the dumped source so the returned handle
carries a stable, idempotent source under subsequent serializations. The
ruamel round-trip dumper is idempotent over the yaml subset this backend
accepts, so re-parsing a mutation output produces the same text again.

Public entry points:

* :class:`YamlStructuralLanguage` -- the :class:`StructuralLanguage` impl.
* :class:`YamlLogicalNameResolver` -- slash/dotted path -> ``.yaml``/``.yml`` file.
* :func:`yaml_kind_schema` -- factory for the yaml kind vocabulary.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

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
class _YamlTree:
    """Opaque parse handle for a yaml document.

    :ivar source: the source text this tree was parsed from, stored verbatim.
        :meth:`YamlStructuralLanguage.serialize` returns this string directly,
        so the round-trip invariant holds for every source the backend
        accepts (an input that doesn't survive ruamel round-trip is rejected
        at :meth:`~YamlStructuralLanguage.parse` time).
    :ivar data: the ruamel round-trip representation (``CommentedMap``,
        ``CommentedSeq``, or a scalar). Used for navigation and mutation.
    """

    source: str
    data: Any


@dataclass
class _YamlDeclaration:
    """An unsealed yaml fragment produced by :meth:`build_declaration`.

    Declarations are intermediate values: they are assembled via
    :func:`build_declaration` and then placed into a tree by
    :meth:`insert_child`. Because yaml's block syntax depends on the
    surrounding indentation, the declaration stores the fragment as raw
    source (no indentation) plus an already-parsed data payload; the payload
    is what the backend actually splices into the parent tree, and it gets
    re-serialized at insert time with the correct indentation.

    :ivar kind: the schema kind this declaration represents.
    :ivar data: the ruamel data payload; used when the declaration is
        inserted into a tree.
    :ivar key: for ``pair`` declarations, the decoded mapping key; ``None``
        otherwise.
    """

    kind: KindName
    data: Any
    key: str | None = None


# --------------------------------------------------------------------------- #
# YAML engine (single shared instance; safe to reuse across parses)
# --------------------------------------------------------------------------- #


def _make_yaml_engine() -> YAML:
    """Build the round-trip yaml engine used for both parse and dump.

    ruamel's ``YAML()`` in round-trip mode preserves comments, quotes and
    ordering. ``preserve_quotes=True`` keeps scalar quoting styles verbatim
    so ``'x'`` doesn't drift to ``x``.
    """
    engine = YAML(typ="rt")
    engine.preserve_quotes = True
    engine.allow_duplicate_keys = False
    # indent(mapping=2, sequence=4, offset=2) matches the conventional yaml
    # style where sequence items under a mapping are indented two spaces from
    # the mapping key (``items:\n  - one``). ruamel's zero-defaults produce
    # unindented items which fail round-trip for that common input shape.
    engine.indent(mapping=2, sequence=4, offset=2)
    # ruamel's default width truncates at 80 cols when re-dumping; widen it so
    # our dumps do not silently fold long scalars the agent has just written.
    engine.width = 10**9
    return engine


_ENGINE = _make_yaml_engine()


def _dump(data: Any) -> str:
    """Dump ``data`` via the shared ruamel engine, returning the string output."""
    buf = StringIO()
    _ENGINE.dump(data, buf)
    return buf.getvalue()


def _load(source: str) -> Any:
    """Load ``source`` via the shared ruamel engine."""
    return _ENGINE.load(source)


# --------------------------------------------------------------------------- #
# Kind schema
# --------------------------------------------------------------------------- #


_ATTR_KEY = AttributeSpec(
    name="key",
    type_hint="str",
    required=True,
    description="mapping key; the backend quotes it only if yaml syntax requires",
)
_ATTR_SCALAR_VALUE = AttributeSpec(
    name="value",
    type_hint="str | int | float | bool | None",
    required=True,
    description="the python value the scalar represents; bool/int/float/None are rendered natively, str is quoted only when ambiguous",
)


def yaml_kind_schema() -> KindSchema:
    """Return the yaml structural kind vocabulary.

    The vocabulary mirrors the yaml value grammar: a ``document`` wraps one
    root value (mapping, sequence, or scalar); mappings contain ``pair``
    children; sequences contain value children directly; ``scalar`` is a
    leaf.
    """
    _VALUE_KINDS = frozenset({"mapping", "sequence", "scalar"})
    _VALUE_PARENT_KINDS = frozenset({"document", "sequence", "pair"})

    document = KindSpec(
        name="document",
        description="A yaml document's top level; wraps exactly one root value.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=_VALUE_KINDS,
    )
    mapping = KindSpec(
        name="mapping",
        description="A yaml mapping (block or flow).",
        attributes=(),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=frozenset({"pair"}),
    )
    sequence = KindSpec(
        name="sequence",
        description="A yaml sequence (block or flow).",
        attributes=(),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=_VALUE_KINDS,
    )
    pair = KindSpec(
        name="pair",
        description="A key/value pair within a yaml mapping.",
        attributes=(_ATTR_KEY,),
        allowed_parent_kinds=frozenset({"mapping"}),
        allowed_child_kinds=_VALUE_KINDS,
    )
    scalar = KindSpec(
        name="scalar",
        description="A yaml scalar value (string, integer, float, boolean, or null).",
        attributes=(_ATTR_SCALAR_VALUE,),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=frozenset(),
    )

    return KindSchema(
        language_key="yaml",
        source_kinds=frozenset({"document"}),
        kinds={
            "document": document,
            "mapping": mapping,
            "sequence": sequence,
            "pair": pair,
            "scalar": scalar,
        },
    )


_YAML_KIND_SCHEMA = yaml_kind_schema()


# --------------------------------------------------------------------------- #
# Logical name resolution
# --------------------------------------------------------------------------- #


_YAML_NAME_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*")


class YamlLogicalNameResolver(LogicalNameResolver):
    """Maps a slashed or dotted logical name to a ``.yaml`` / ``.yml`` file.

    The resolver is file-level: it identifies which yaml file a logical name
    points at. Inside-file navigation is handled by :func:`walk_symbols`, not
    by this resolver.

    :ivar _project_root: project root; resolutions report paths relative to it.
    :ivar _source_roots: ordered content directories to probe.
    :ivar _extensions: file extensions to try, in order (first match wins on
        lookup; first entry is used when creating a new file).
    """

    _DEFAULT_EXTENSIONS: tuple[str, ...] = (".yaml", ".yml")

    def __init__(
        self,
        project_root: Path,
        source_roots: Sequence[Path] = (),
        extensions: Sequence[str] = (),
    ):
        """:param project_root: directory at whose root paths are reported.
        :param source_roots: directories under which yaml files live;
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
            if not part or not _YAML_NAME_PART.fullmatch(part):
                raise NameResolutionError(raw, f"invalid yaml name part: {part!r}")
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


def _value_kind(node: Any) -> KindName:
    """Return the schema kind name for a yaml value node."""
    if isinstance(node, CommentedMap | dict):
        return "mapping"
    if isinstance(node, CommentedSeq | list):
        return "sequence"
    return "scalar"


def _stringify_key(key: Any) -> str:
    """Render a mapping key as its string form for name-path use."""
    # ruamel represents keys using their python type; bool/int/float/None all
    # get stringified for the name-path so agents can address non-string keys.
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    return str(key)


def _walk(node: Any, prefix: str) -> Iterable[tuple[str, KindName, Any]]:
    """Yield addressable symbols under ``node`` with name paths built on ``prefix``.

    The walk yields every mapping pair and every sequence element. Compound
    values (mappings, sequences) are walked recursively. Scalar values
    directly under a pair are not yielded separately -- the pair itself is
    the addressable symbol.
    """
    if isinstance(node, CommentedMap | dict):
        for key in node:
            rendered = _stringify_key(key)
            pair_path = f"{prefix}/{rendered}" if prefix else rendered
            value = node[key]
            yield pair_path, "pair", (node, key)
            yield from _walk(value, pair_path)
        return

    if isinstance(node, CommentedSeq | list):
        for index, item in enumerate(node):
            segment = f"[{index}]"
            item_path = f"{prefix}/{segment}" if prefix else segment
            yield item_path, _value_kind(item), item
            yield from _walk(item, item_path)
        return

    # scalars contribute no addressable symbols of their own
    return


def walk_symbols(tree: _YamlTree) -> Iterable[tuple[str, KindName, Any]]:
    """Yield ``(name_path, kind, node)`` for every addressable yaml symbol in ``tree``.

    Mapping pairs and sequence elements are addressable. Name paths are
    slash-separated; sequence elements use ``[N]`` segments. The root
    document and its root value carry no name path and are not yielded;
    only pairs and elements below them are.

    :raises TypeError: if ``tree`` is not a :class:`_YamlTree`.
    """
    if not isinstance(tree, _YamlTree):
        raise TypeError(f"walk_symbols expects a _YamlTree, got {type(tree).__name__}")
    return list(_walk(tree.data, ""))


# --------------------------------------------------------------------------- #
# Pattern matching
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _YamlPattern:
    """A compiled yaml pattern: the pattern source plus its loaded tree."""

    source: str
    data: Any


@dataclass(frozen=True)
class _YamlReplacement:
    """A rendered replacement fragment ready for :meth:`apply_replacement`."""

    data: Any


_CAPTURE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _capture_kind(literal: str) -> tuple[str, str] | None:
    """Classify a scalar literal as a capture sigil.

    Captures are embedded inside yaml strings:

    * ``"$_"`` -- a wildcard that matches any value.
    * ``"$name"`` -- a capture that matches any value and binds it under
      ``name``.

    Returns ``(kind, name)`` where ``kind`` is ``"wildcard"`` or ``"capture"``
    and ``name`` is the capture identifier (``"_"`` for wildcards). Returns
    ``None`` when the literal is not a sigil.
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
    """Yield ``node`` and every nested yaml value node in document order."""
    yield node
    if isinstance(node, CommentedMap | dict):
        for key in node:
            yield from _iter_value_nodes(node[key])
        return
    if isinstance(node, CommentedSeq | list):
        for item in node:
            yield from _iter_value_nodes(item)


def _match_value(pattern: Any, target: Any, bindings: dict[str, Any]) -> bool:
    """Match ``pattern`` against ``target``, recording captures into ``bindings``.

    Matching rules:

    * A string scalar ``"$_"`` matches any value without binding.
    * A string scalar ``"$name"`` matches any value and binds it under
      ``name``. Re-binding to a different value fails the match.
    * Mappings match when key sets are equal (order-independent) and values
      match recursively.
    * Sequences match when lengths are equal and items match in order.
    * Other scalars match when equal under Python's ``==``.
    """
    if isinstance(pattern, str):
        sigil = _capture_kind(pattern)
        if sigil is not None:
            kind, name = sigil
            if kind == "wildcard":
                return True
            # capture: bind or check consistency with an earlier binding
            existing = bindings.get(name)
            if existing is None:
                bindings[name] = target
                return True
            return existing == target

    if isinstance(pattern, CommentedMap | dict):
        if not isinstance(target, CommentedMap | dict):
            return False
        if set(pattern.keys()) != set(target.keys()):
            return False
        for key in pattern:
            if not _match_value(pattern[key], target[key], bindings):
                return False
        return True

    if isinstance(pattern, CommentedSeq | list):
        if not isinstance(target, CommentedSeq | list):
            return False
        if len(pattern) != len(target):
            return False
        for p_item, t_item in zip(pattern, target, strict=False):
            if not _match_value(p_item, t_item, bindings):
                return False
        return True

    # everything else (int, float, bool, None): value equality
    return pattern == target


def _render_capture_resolution(node: Any, bindings: Mapping[str, Any]) -> Any:
    """Substitute capture references in a replacement tree with their bindings.

    Walks the loaded replacement data and, wherever a scalar ``"$name"`` is
    found, replaces it with ``bindings[name]``. Mappings and sequences are
    rebuilt with substituted children so the original replacement tree is not
    mutated.
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

    if isinstance(node, CommentedMap | dict):
        rebuilt: dict[Any, Any] = {}
        for key in node:
            rebuilt[key] = _render_capture_resolution(node[key], bindings)
        return rebuilt

    if isinstance(node, CommentedSeq | list):
        return [_render_capture_resolution(item, bindings) for item in node]

    return node


def _replace_in_tree(root: Any, target: Any, replacement: Any) -> tuple[Any, bool]:
    """Return a copy of ``root`` with ``target`` replaced by ``replacement``.

    The replacement is located by object identity (``is``). Returns a
    ``(new_root, found)`` pair so callers can detect failure without
    raising from deep inside the recursion.
    """
    if root is target:
        return replacement, True

    if isinstance(root, CommentedMap | dict):
        for key in list(root.keys()):
            new_value, found = _replace_in_tree(root[key], target, replacement)
            if found:
                root[key] = new_value
                return root, True
        return root, False

    if isinstance(root, CommentedSeq | list):
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
# YamlStructuralLanguage
# --------------------------------------------------------------------------- #


class YamlStructuralLanguage(StructuralLanguage):
    """Structural backend for yaml documents.

    Parses with :mod:`ruamel.yaml` in round-trip mode. The parse handle stores
    the source verbatim, so ``serialize(parse(s)) == s`` is satisfied by
    returning the stored source. Any source that doesn't round-trip through
    ruamel is rejected at parse time so the backend only accepts inputs for
    which the invariant holds.

    Mutations modify the ruamel data tree in place (or on a deep copy),
    re-dump to yaml, and re-parse the dumped text. Because the ruamel
    round-trip dumper is idempotent on its own output, the new handle
    satisfies the invariant again.
    """

    def __init__(self, name_resolver: LogicalNameResolver | None = None):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
        Defaults to a :class:`YamlLogicalNameResolver` rooted at the current
        working directory.
        """
        self._name_resolver = name_resolver or YamlLogicalNameResolver(Path.cwd())

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "yaml"

    @property
    def kind_schema(self) -> KindSchema:
        return _YAML_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- parse / serialize -------------------------------------------------

    def parse(self, source: str) -> _YamlTree:
        try:
            data = _load(source)
        except YAMLError as err:
            raise ParseError(language_key="yaml", source_preview=source[:240], detail=str(err)) from err

        # enforce the round-trip invariant by re-dumping and comparing; inputs
        # whose formatting doesn't survive a dump are rejected so the backend
        # only accepts sources for which serialize(parse(s)) == s holds
        dumped = _dump(data) if data is not None else ""
        # empty source: ruamel dumps None as "null\n...\n"; accept it only when
        # the input is empty or whitespace and store the empty source verbatim
        if source.strip() == "":
            if data is not None:
                raise ParseError(
                    language_key="yaml",
                    source_preview=source[:240],
                    detail="whitespace-only source must parse to None",
                )
            return _YamlTree(source=source, data=None)

        if dumped != source:
            raise ParseError(
                language_key="yaml",
                source_preview=source[:240],
                detail=(
                    f"source does not round-trip through ruamel.yaml round-trip mode; re-dump differs. expected={source!r} got={dumped!r}"
                ),
            )
        return _YamlTree(source=source, data=data)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _YamlTree):
            return tree.source
        if isinstance(tree, _YamlDeclaration):
            # declarations are fragments; render their data via ruamel
            return _dump(tree.data) if tree.data is not None else ""
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _YamlTree):
            raise TypeError(f"root_kind expects a _YamlTree, got {type(tree).__name__}")
        return "document"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        return walk_symbols(tree)

    # ---- declaration -------------------------------------------------------

    def build_declaration(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
        children: Iterable[Any],
    ) -> _YamlDeclaration:
        self.kind_schema.get(kind)  # validates the kind name exists
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
            return _YamlDeclaration(kind=kind, data=value)

        if kind == "pair":
            key = _require_str(attributes, "key", kind)
            if len(children_list) != 1:
                raise DeclarationError(kind, f"pair requires exactly one value child, got {len(children_list)}")
            value_child = children_list[0]
            if not isinstance(value_child, _YamlDeclaration):
                raise DeclarationError(kind, f"pair child must be a _YamlDeclaration from this backend; got {type(value_child).__name__}")
            return _YamlDeclaration(kind=kind, data=value_child.data, key=key)

        if kind == "mapping":
            mapping_data: CommentedMap = CommentedMap()
            for idx, child in enumerate(children_list):
                if not isinstance(child, _YamlDeclaration) or child.kind != "pair":
                    raise DeclarationError(kind, f"mapping child {idx} must be a pair declaration, got {type(child).__name__}")
                if child.key is None:
                    raise DeclarationError(kind, f"mapping child {idx} is missing its key")
                if child.key in mapping_data:
                    raise DeclarationError(kind, f"duplicate key {child.key!r} in mapping")
                mapping_data[child.key] = child.data
            return _YamlDeclaration(kind=kind, data=mapping_data)

        if kind == "sequence":
            sequence_data: CommentedSeq = CommentedSeq()
            for idx, child in enumerate(children_list):
                if not isinstance(child, _YamlDeclaration):
                    raise DeclarationError(kind, f"sequence child {idx} must be a _YamlDeclaration, got {type(child).__name__}")
                if child.kind == "pair":
                    raise DeclarationError(kind, f"sequence child {idx} must be a value, not a pair")
                sequence_data.append(child.data)
            return _YamlDeclaration(kind=kind, data=sequence_data)

        raise DeclarationError(kind, f"build_declaration not supported for kind {kind!r}")

    # ---- insert / remove ---------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _YamlTree:
        if not isinstance(parent, _YamlTree):
            raise TypeError(f"parent must be _YamlTree, got {type(parent).__name__}")
        if not isinstance(child, _YamlDeclaration):
            raise TypeError(f"child must be a _YamlDeclaration, got {type(child).__name__}")
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")

        root = parent.data
        new_root: Any
        if isinstance(root, CommentedMap | dict):
            if child.kind != "pair":
                raise TypeError(f"inserting into mapping requires a pair declaration, got kind {child.kind!r}")
            if child.key is None:
                raise DeclarationError(child.kind, "pair declaration is missing its key")
            if child.key in root:
                raise DeclarationError(child.kind, f"duplicate key {child.key!r} in mapping")
            new_root = _mapping_with_inserted(root, child.key, child.data, anchor, position)
            return self.parse(_dump(new_root))

        if isinstance(root, CommentedSeq | list):
            if child.kind == "pair":
                raise TypeError("inserting into sequence requires a value declaration, not a pair")
            new_root = _sequence_with_inserted(root, child.data, anchor, position)
            return self.parse(_dump(new_root))

        raise ValueError(f"cannot insert into scalar root of kind {_value_kind(root)!r}")

    def remove_child(self, parent: Any, child: Any) -> _YamlTree:
        if not isinstance(parent, _YamlTree):
            raise TypeError(f"parent must be _YamlTree, got {type(parent).__name__}")
        root = parent.data
        new_root: Any
        if isinstance(root, CommentedMap | dict):
            new_root = _mapping_without(root, child)
            return self.parse(_dump(new_root))
        if isinstance(root, CommentedSeq | list):
            new_root = _sequence_without(root, child)
            return self.parse(_dump(new_root))
        raise ValueError(f"cannot remove from scalar root of kind {_value_kind(root)!r}")

    # ---- pattern matching & rewriting --------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError("parse", "pattern source is empty")
        try:
            data = _load(pattern_source)
        except YAMLError as err:
            raise PatternError("parse", f"pattern source is not valid yaml: {err}") from err
        if data is None:
            raise PatternError("parse", "pattern source evaluates to null; cannot match anything")
        return _YamlPattern(source=pattern_source, data=data)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _YamlTree):
            raise TypeError(f"tree must be a _YamlTree, got {type(tree).__name__}")
        if not isinstance(pattern, _YamlPattern):
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
    ) -> _YamlReplacement:
        if not replacement_source.strip():
            raise PatternError("parse", "replacement source is empty")
        try:
            data = _load(replacement_source)
        except YAMLError as err:
            raise PatternError("parse", f"replacement source is not valid yaml: {err}") from err
        if data is None:
            raise PatternError("parse", "replacement evaluates to null; must be a yaml value")
        resolved = _render_capture_resolution(data, dict(bindings))
        return _YamlReplacement(data=resolved)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _YamlTree:
        if not isinstance(tree, _YamlTree):
            raise TypeError(f"tree must be a _YamlTree, got {type(tree).__name__}")
        if not isinstance(replacement, _YamlReplacement):
            raise TypeError(f"replacement must come from this backend's render_replacement, got {type(replacement).__name__}")
        # deep-copy the data so the original tree is not mutated
        import copy as _copy

        cloned = _copy.deepcopy(tree.data)
        # locate the matching node in the clone by position rather than identity,
        # because deepcopy changed the object identities from match.node
        target = _locate_by_path(tree.data, cloned, match.node)
        if target is None:
            raise ValueError("match.node was not found in tree")
        new_root, found = _replace_in_tree(cloned, target, replacement.data)
        if not found:
            raise ValueError("match.node was not found in tree")
        if new_root is target:
            # replacement at root
            return self.parse(_dump(replacement.data))
        return self.parse(_dump(new_root))

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _YamlTree:
        if source_kind != "document":
            raise DeclarationError(source_kind, f"yaml has no source kind {source_kind!r}")
        # an empty yaml source parses to None under ruamel; use that as the seed
        return _YamlTree(source="", data=None)

    def container_insert_member(
        self,
        tree: Any,
        anchor_or_container_path: str,
        source: str,
        position: str = "end",
    ) -> _YamlTree:
        # ABC-level shape and arg validation
        if not isinstance(tree, _YamlTree):
            raise TypeError(f"tree must be _YamlTree, got {type(tree).__name__}")
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")

        # start/end target the container directly; before/after target a sibling
        if position in {"start", "end"}:
            container_path = anchor_or_container_path
            anchor_segment: str | None = None
        else:
            container_path, anchor_segment = _split_member_path_yaml(anchor_or_container_path)

        # deepcopy once, then walk + mutate the live clone so surrounding commentary stays anchored
        cloned_root = copy.deepcopy(tree.data)
        container = _resolve_container_yaml(cloned_root, container_path)

        # dispatch on container kind
        if isinstance(container, CommentedMap | dict):
            key_str, value = _parse_yaml_member(source)
            existing_keys = [_stringify_key(k) for k in container]
            if key_str in existing_keys:
                raise DeclarationError("pair", f"duplicate key {key_str!r} in mapping")
            _mapping_insert_yaml_in_place(container, key_str, value, anchor_segment, position)
        elif isinstance(container, CommentedSeq | list):
            anchor_index: int | None = None
            if anchor_segment is not None:
                anchor_index = _parse_sequence_index_segment_yaml(anchor_segment)
            value = _parse_yaml_value(source)
            _sequence_insert_yaml_in_place(container, value, anchor_index, position)
        else:
            raise ValueError(
                f"container at {container_path!r} is not a mapping or sequence",
            )

        return self.parse(_dump(cloned_root))

    def container_remove_member(self, tree: Any, member_path: str) -> _YamlTree:
        if not isinstance(tree, _YamlTree):
            raise TypeError(f"tree must be _YamlTree, got {type(tree).__name__}")

        parent_path, last_segment = _split_member_path_yaml(member_path)
        cloned_root = copy.deepcopy(tree.data)
        container = _resolve_container_yaml(cloned_root, parent_path)

        if isinstance(container, CommentedMap | dict):
            _mapping_remove_yaml_in_place(container, last_segment)
        elif isinstance(container, CommentedSeq | list):
            idx = _parse_sequence_index_segment_yaml(last_segment)
            _sequence_remove_yaml_in_place(container, idx)
        else:
            raise ValueError(
                f"container at {parent_path!r} is not a mapping or sequence",
            )

        return self.parse(_dump(cloned_root))

    def container_replace_member(self, tree: Any, member_path: str, source: str) -> _YamlTree:
        if not isinstance(tree, _YamlTree):
            raise TypeError(f"tree must be _YamlTree, got {type(tree).__name__}")

        parent_path, last_segment = _split_member_path_yaml(member_path)
        cloned_root = copy.deepcopy(tree.data)
        container = _resolve_container_yaml(cloned_root, parent_path)
        new_value = _parse_yaml_value(source)

        if isinstance(container, CommentedMap | dict):
            _mapping_replace_yaml_in_place(container, last_segment, new_value)
        elif isinstance(container, CommentedSeq | list):
            idx = _parse_sequence_index_segment_yaml(last_segment)
            _sequence_replace_yaml_in_place(container, idx, new_value)
        else:
            raise ValueError(
                f"container at {parent_path!r} is not a mapping or sequence",
            )

        return self.parse(_dump(cloned_root))


# --------------------------------------------------------------------------- #
# Mutation helpers
# --------------------------------------------------------------------------- #


def _mapping_with_inserted(
    mapping: Any,
    key: str,
    value: Any,
    anchor: Any,
    position: str,
) -> CommentedMap:
    """Return a copy of ``mapping`` with ``(key, value)`` inserted at ``position``."""
    import copy as _copy

    clone = _copy.deepcopy(mapping)
    # build a new CommentedMap preserving original order, splicing the new pair
    result = CommentedMap()

    # for position in {"start","end"}, anchor is ignored
    if position == "start":
        result[key] = _copy.deepcopy(value)
        for k in clone:
            result[k] = clone[k]
        return result
    if position == "end":
        for k in clone:
            result[k] = clone[k]
        result[key] = _copy.deepcopy(value)
        return result

    # before/after: anchor is (node, anchor_key); locate anchor_key in the clone
    anchor_key = _mapping_anchor_key(mapping, anchor)
    if anchor_key is None:
        raise ValueError("anchor not found in mapping")
    for k in clone:
        if position == "before" and k == anchor_key:
            result[key] = _copy.deepcopy(value)
        result[k] = clone[k]
        if position == "after" and k == anchor_key:
            result[key] = _copy.deepcopy(value)
    return result


def _mapping_anchor_key(mapping: Any, anchor: Any) -> Any:
    """Return the mapping key an anchor identifies.

    Anchors in the yaml backend come from :func:`walk_symbols`, which yields
    pair tuples ``(parent, key)``. This helper accepts either that tuple form
    or a raw key already present in ``mapping``.
    """
    if isinstance(anchor, tuple) and len(anchor) == 2:
        parent, key = anchor
        if parent is mapping and key in mapping:
            return key
        # fall through to try key-in-mapping directly
        if key in mapping:
            return key
        return None
    if isinstance(anchor, str) and anchor in mapping:
        return anchor
    return None


def _mapping_without(mapping: Any, child: Any) -> CommentedMap:
    import copy as _copy

    clone = _copy.deepcopy(mapping)
    key = _mapping_anchor_key(mapping, child)
    if key is None:
        raise ValueError("child not found in mapping")
    del clone[key]
    return clone


def _sequence_with_inserted(
    sequence: Any,
    value: Any,
    anchor: Any,
    position: str,
) -> CommentedSeq:
    import copy as _copy

    clone = _copy.deepcopy(sequence)
    # item deepcopy so subsequent mutations of the caller's handle do not ripple
    v = _copy.deepcopy(value)

    if position == "start":
        clone.insert(0, v)
        return clone
    if position == "end":
        clone.append(v)
        return clone

    index = _sequence_anchor_index(sequence, anchor)
    if index is None:
        raise ValueError("anchor not found in sequence")
    if position == "before":
        clone.insert(index, v)
    else:  # after
        clone.insert(index + 1, v)
    return clone


def _sequence_anchor_index(sequence: Any, anchor: Any) -> int | None:
    """Return the index of ``anchor`` inside ``sequence``, or ``None``."""
    if not isinstance(sequence, CommentedSeq | list):
        return None
    for i in range(len(sequence)):
        if sequence[i] is anchor:
            return i
    return None


def _sequence_without(sequence: Any, child: Any) -> CommentedSeq:
    import copy as _copy

    clone = _copy.deepcopy(sequence)
    index = _sequence_anchor_index(sequence, child)
    if index is None:
        raise ValueError("child not found in sequence")
    del clone[index]
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
    if isinstance(original, CommentedMap | dict):
        for key in original:
            found = _locate_by_path(original[key], cloned[key], target)
            if found is not None:
                return found
        return None
    if isinstance(original, CommentedSeq | list):
        for i in range(len(original)):
            found = _locate_by_path(original[i], cloned[i], target)
            if found is not None:
                return found
        return None
    return None


# -----------------------------------------------------------------------------
# Path-based container-member helpers
#
# these power the L3 container_insert_member / container_remove_member /
# container_replace_member ABC methods on YamlStructuralLanguage. strategy
# is the same as the TOML backend: deepcopy the root once, walk the
# slash-separated name-path to the target container inside the clone, then
# mutate that container in place. ruamel.yaml CommentedMap / CommentedSeq
# preserve comments and layout across edits as long as we manipulate the
# live nodes rather than rebuilding.
# -----------------------------------------------------------------------------


def _split_member_path_yaml(member_path: str) -> tuple[str, str]:
    """Split a member path on its last ``/``.

    ``"foo/bar/baz"`` -> ``("foo/bar", "baz")``. A path with no ``/`` is
    returned as ``("", member_path)`` -- the member is a direct child of
    the document root.
    """
    slash_idx = member_path.rfind("/")
    if slash_idx < 0:
        return "", member_path
    return member_path[:slash_idx], member_path[slash_idx + 1 :]


def _parse_sequence_index_segment_yaml(segment: str) -> int:
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


def _resolve_node_by_path_yaml(root: Any, path: str) -> Any:
    """Walk ``root`` along ``path`` and return the terminal node.

    Mapping-key segments descend into the mapping's VALUE for that key
    (matching :func:`walk_symbols`). Bracketed segments are sequence
    indices.

    :raises ValueError: on empty path, type mismatches, or unresolvable
        segments.
    """
    if not path:
        raise ValueError("empty path does not resolve to a node")
    node: Any = root
    for segment in path.split("/"):
        if segment.startswith("[") and segment.endswith("]"):
            # sequence-index descent
            if not isinstance(node, CommentedSeq | list):
                raise ValueError(
                    f"segment {segment!r} expects a sequence, got {type(node).__name__}",
                )
            idx = _parse_sequence_index_segment_yaml(segment)
            if idx < 0 or idx >= len(node):
                raise ValueError(
                    f"sequence index {idx} out of range for segment {segment!r}",
                )
            node = node[idx]
        else:
            # mapping-key descent
            if not isinstance(node, CommentedMap | dict):
                raise ValueError(
                    f"segment {segment!r} expects a mapping, got {type(node).__name__}",
                )
            match_key = None
            for k in node:
                if _stringify_key(k) == segment:
                    match_key = k
                    break
            if match_key is None:
                raise ValueError(f"mapping has no key {segment!r}")
            node = node[match_key]
    return node


def _resolve_container_yaml(root: Any, container_path: str) -> Any:
    """Resolve ``container_path`` against ``root``, requiring a container node.

    Empty path returns ``root`` itself. Any non-empty path must land on a
    :class:`CommentedMap` / :class:`dict` or on a :class:`CommentedSeq` /
    :class:`list`.
    """
    if not container_path:
        return root
    node = _resolve_node_by_path_yaml(root, container_path)
    if isinstance(node, CommentedMap | dict | CommentedSeq | list):
        return node
    raise ValueError(
        f"path {container_path!r} does not resolve to a YAML container",
    )


def _parse_yaml_member(source: str) -> tuple[str, Any]:
    """Parse a single ``key: value`` pair and return ``(key_str, value)``.

    YAML parses a pair fragment into a one-entry mapping; this helper
    insists on exactly one entry and returns its key/value.
    """
    data = _load(source)
    if not isinstance(data, CommentedMap | dict):
        raise ParseError(
            language_key="yaml",
            source_preview=source[:240],
            detail="member source must be a key: value pair",
        )
    if len(data) != 1:
        raise ParseError(
            language_key="yaml",
            source_preview=source[:240],
            detail=f"member source must contain exactly one pair, got {len(data)}",
        )
    for k, v in data.items():
        return _stringify_key(k), v
    # unreachable given the len check above
    raise ParseError(
        language_key="yaml",
        source_preview=source[:240],
        detail="empty member source",
    )


def _parse_yaml_value(source: str) -> Any:
    """Parse a bare YAML value expression (for sequence items or replacement RHS)."""
    return _load(source)


def _mapping_insert_yaml_in_place(
    mapping: Any,
    key_str: str,
    value: Any,
    anchor_key: str | None,
    position: str,
) -> None:
    """Splice a ``(key, value)`` pair into ``mapping`` at ``position`` in place.

    :class:`CommentedMap` exposes an ``insert(pos, key, value)`` method
    that preserves surrounding commentary; we use it for all four
    positions. Plain ``dict`` (YAML-safe fallback) also supports
    positional insertion via a rebuild, but in the round-trip engine the
    root is always :class:`CommentedMap`.
    """
    if position == "start":
        mapping.insert(0, key_str, value)
        return
    if position == "end":
        # len() on CommentedMap returns the member count; inserting at the
        # end places the new pair after every existing pair
        mapping.insert(len(mapping), key_str, value)
        return
    if anchor_key is None:
        raise ValueError(f"position {position!r} requires an anchor")
    idx = _find_mapping_key_index(mapping, anchor_key)
    if idx is None:
        raise ValueError(f"anchor key {anchor_key!r} not found in mapping")
    if position == "before":
        mapping.insert(idx, key_str, value)
    else:  # after
        mapping.insert(idx + 1, key_str, value)


def _find_mapping_key_index(mapping: Any, key_str: str) -> int | None:
    """Return the positional index of ``key_str`` in ``mapping``'s iteration order, or ``None``."""
    for i, k in enumerate(mapping):
        if _stringify_key(k) == key_str:
            return i
    return None


def _mapping_remove_yaml_in_place(mapping: Any, key_str: str) -> None:
    """Delete the entry keyed by ``key_str`` from ``mapping``."""
    target_key = None
    for k in mapping:
        if _stringify_key(k) == key_str:
            target_key = k
            break
    if target_key is None:
        raise ValueError(f"key {key_str!r} not found in mapping")
    del mapping[target_key]


def _mapping_replace_yaml_in_place(mapping: Any, key_str: str, new_value: Any) -> None:
    """Replace the value of the entry keyed by ``key_str``, preserving key identity and position."""
    target_key = None
    for k in mapping:
        if _stringify_key(k) == key_str:
            target_key = k
            break
    if target_key is None:
        raise ValueError(f"key {key_str!r} not found in mapping")
    mapping[target_key] = new_value


def _sequence_insert_yaml_in_place(
    sequence: Any,
    value: Any,
    anchor_index: int | None,
    position: str,
) -> None:
    """Insert ``value`` into a :class:`CommentedSeq` / :class:`list` at ``position`` in place."""
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


def _sequence_remove_yaml_in_place(sequence: Any, index: int) -> None:
    """Remove the item at ``index`` from ``sequence``."""
    if index < 0 or index >= len(sequence):
        raise ValueError(f"sequence index {index} out of range")
    del sequence[index]


def _sequence_replace_yaml_in_place(sequence: Any, index: int, new_value: Any) -> None:
    """Replace the item at ``index`` in ``sequence`` with ``new_value``."""
    if index < 0 or index >= len(sequence):
        raise ValueError(f"sequence index {index} out of range")
    sequence[index] = new_value


__all__ = [
    "YamlLogicalNameResolver",
    "YamlStructuralLanguage",
    "walk_symbols",
    "yaml_kind_schema",
]
