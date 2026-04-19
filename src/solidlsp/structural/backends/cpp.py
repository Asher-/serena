"""C++ :class:`StructuralLanguage` backend using libclang.

Unlike :mod:`solidlsp.structural.backends.python` — which leans on libcst's
value-typed CST nodes — this backend uses libclang's :class:`TranslationUnit`
only for read access. All mutation happens in pure Python as offset-anchored
source edits: :class:`libclang.cindex.Cursor` exposes ``.extent`` (byte ranges
into the original source buffer) and ``.location.offset`` (the spelling
location), which is everything we need to emit edits at arbitrary AST-anchored
points.

Why not :class:`libclang.cindex.Rewriter`? libclang's C API exposes
``clang_CXRewriter_*`` from LLVM 9 onward, but the :mod:`clang.cindex` Python
binding shipped with the ``libclang`` wheel does not bind those entry points.
A pure-Python offset rewriter is simpler anyway: byte-identical round-trip is
the zero-edit case; every edit is ``(offset, length, replacement)`` applied in
reverse-offset order so earlier offsets never shift.

Public entry points:

* :class:`CppStructuralLanguage` — the :class:`StructuralLanguage` impl.
* :class:`CppLogicalNameResolver` — dotted or ``::``-delimited namespace path
  → header/source file under configured roots.
* :func:`cpp_kind_schema` — factory for the C++ kind vocabulary.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import clang.cindex as cx

from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.errors import (
    DeclarationError,
    NameResolutionError,
    ParseError,
    PatternError,
)
from solidlsp.structural.kinds import (
    AttributeSpec,
    KindName,
    KindSchema,
    KindSpec,
)
from solidlsp.structural.names import LogicalName, LogicalNameResolver, NameResolution
from solidlsp.structural.patterns import AstPattern, PatternMatch

# =============================================================================
# Kind schema
# =============================================================================

_ATTR_NAME = AttributeSpec(
    name="name",
    type_hint="str",
    required=True,
    description="the identifier introduced by this declaration",
)
_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=False,
    description="raw body source between the opening and closing braces (no outer braces)",
)
_ATTR_STATEMENT = AttributeSpec(
    name="statement",
    type_hint="str",
    required=True,
    description="full source of the statement/directive exactly as it should appear",
)
_ATTR_BASES = AttributeSpec(
    name="bases",
    type_hint="list[str]",
    required=False,
    description="raw source of each base-clause entry (e.g. 'public Foo', 'private virtual Bar<T>')",
)
_ATTR_PARAMS = AttributeSpec(
    name="parameters",
    type_hint="str",
    required=False,
    description="raw parameter-list source (without outer parentheses), e.g. 'const std::string& who, int n = 0'",
)
_ATTR_RETURN = AttributeSpec(
    name="return_type",
    type_hint="str",
    required=True,
    description="raw return-type source (e.g. 'int', 'std::vector<T>', 'auto')",
)
_ATTR_TEMPLATE_PARAMS = AttributeSpec(
    name="template_parameters",
    type_hint="str | None",
    required=False,
    description="raw template-parameter source (without outer angle brackets), e.g. 'typename T, int N'",
)
_ATTR_QUALIFIERS = AttributeSpec(
    name="qualifiers",
    type_hint="str",
    required=False,
    description="raw qualifier source that follows the signature (e.g. 'const', 'noexcept', '= default')",
)
_ATTR_TYPE = AttributeSpec(
    name="type",
    type_hint="str",
    required=True,
    description="raw declared-type source",
)
_ATTR_INITIALIZER = AttributeSpec(
    name="initializer",
    type_hint="str | None",
    required=False,
    description="raw initializer source (without the leading '=' or braces)",
)


def cpp_kind_schema() -> KindSchema:
    """Return the C++ structural kind vocabulary.

    :return: the kind schema exposed by :class:`CppStructuralLanguage`.
    """
    # block: source-root
    tu = KindSpec(
        name="translation_unit",
        description="A C or C++ source file's top level.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(
            {
                "include",
                "namespace",
                "class",
                "struct",
                "union",
                "function",
                "variable",
                "type_alias",
                "enum",
            }
        ),
    )

    # block: preprocessor directive and structural container
    include = KindSpec(
        name="include",
        description="An '#include' directive.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"translation_unit"}),
        allowed_child_kinds=frozenset(),
    )
    namespace = KindSpec(
        name="namespace",
        description="A namespace definition.",
        attributes=(_ATTR_NAME, _ATTR_BODY),
        allowed_parent_kinds=frozenset({"translation_unit", "namespace"}),
        allowed_child_kinds=frozenset(
            {
                "namespace",
                "class",
                "struct",
                "union",
                "function",
                "variable",
                "type_alias",
                "enum",
            }
        ),
    )

    # block: record declarations share a schema (class/struct/union)
    record_allowed_children = frozenset(
        {
            "method",
            "field",
            "type_alias",
            "class",
            "struct",
            "union",
            "enum",
        }
    )
    record_allowed_parents = frozenset(
        {"translation_unit", "namespace", "class", "struct", "union"}
    )
    record_attributes = (
        _ATTR_NAME,
        _ATTR_BASES,
        _ATTR_BODY,
        _ATTR_TEMPLATE_PARAMS,
    )
    cls = KindSpec(
        name="class",
        description="A class definition (private by default).",
        attributes=record_attributes,
        allowed_parent_kinds=record_allowed_parents,
        allowed_child_kinds=record_allowed_children,
    )
    struct = KindSpec(
        name="struct",
        description="A struct definition (public by default).",
        attributes=record_attributes,
        allowed_parent_kinds=record_allowed_parents,
        allowed_child_kinds=record_allowed_children,
    )
    union = KindSpec(
        name="union",
        description="A union definition.",
        attributes=record_attributes,
        allowed_parent_kinds=record_allowed_parents,
        allowed_child_kinds=record_allowed_children - {"class", "struct", "union"},
    )

    # block: callable declarations
    function = KindSpec(
        name="function",
        description="A free function or namespace-scope function definition.",
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_QUALIFIERS,
            _ATTR_TEMPLATE_PARAMS,
        ),
        allowed_parent_kinds=frozenset({"translation_unit", "namespace"}),
        allowed_child_kinds=frozenset(),
    )
    method = KindSpec(
        name="method",
        description="A member function (including constructors, destructors, operators).",
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_QUALIFIERS,
            _ATTR_TEMPLATE_PARAMS,
        ),
        allowed_parent_kinds=frozenset({"class", "struct", "union"}),
        allowed_child_kinds=frozenset(),
    )

    # block: value declarations
    field_spec = KindSpec(
        name="field",
        description="A non-static data member of a class, struct, or union.",
        attributes=(_ATTR_NAME, _ATTR_TYPE, _ATTR_INITIALIZER),
        allowed_parent_kinds=frozenset({"class", "struct", "union"}),
        allowed_child_kinds=frozenset(),
    )
    variable = KindSpec(
        name="variable",
        description="A namespace-scope variable definition.",
        attributes=(_ATTR_NAME, _ATTR_TYPE, _ATTR_INITIALIZER),
        allowed_parent_kinds=frozenset({"translation_unit", "namespace"}),
        allowed_child_kinds=frozenset(),
    )

    # block: type-level declarations
    type_alias = KindSpec(
        name="type_alias",
        description="A 'using X = ...;' or 'typedef ... X;' declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset(
            {"translation_unit", "namespace", "class", "struct", "union"}
        ),
        allowed_child_kinds=frozenset(),
    )
    enum = KindSpec(
        name="enum",
        description="An enum or enum-class definition.",
        attributes=(_ATTR_NAME, _ATTR_BODY),
        allowed_parent_kinds=frozenset(
            {"translation_unit", "namespace", "class", "struct", "union"}
        ),
        allowed_child_kinds=frozenset({"enum_constant"}),
    )
    enum_constant = KindSpec(
        name="enum_constant",
        description="A single enumerator within an enum definition.",
        attributes=(_ATTR_NAME, _ATTR_INITIALIZER),
        allowed_parent_kinds=frozenset({"enum"}),
        allowed_child_kinds=frozenset(),
    )

    return KindSchema(
        language_key="cpp",
        source_kinds=frozenset({"translation_unit"}),
        kinds={
            "translation_unit": tu,
            "include": include,
            "namespace": namespace,
            "class": cls,
            "struct": struct,
            "union": union,
            "function": function,
            "method": method,
            "field": field_spec,
            "variable": variable,
            "type_alias": type_alias,
            "enum": enum,
            "enum_constant": enum_constant,
        },
    )


_CPP_KIND_SCHEMA = cpp_kind_schema()


# cursor-kind → structural kind name
_CURSOR_KIND_TO_STRUCTURAL: Mapping[cx.CursorKind, KindName] = {
    cx.CursorKind.INCLUSION_DIRECTIVE: "include",
    cx.CursorKind.NAMESPACE: "namespace",
    cx.CursorKind.CLASS_DECL: "class",
    cx.CursorKind.CLASS_TEMPLATE: "class",
    cx.CursorKind.STRUCT_DECL: "struct",
    cx.CursorKind.UNION_DECL: "union",
    cx.CursorKind.FUNCTION_DECL: "function",
    cx.CursorKind.FUNCTION_TEMPLATE: "function",
    cx.CursorKind.CXX_METHOD: "method",
    cx.CursorKind.CONSTRUCTOR: "method",
    cx.CursorKind.DESTRUCTOR: "method",
    cx.CursorKind.CONVERSION_FUNCTION: "method",
    cx.CursorKind.FIELD_DECL: "field",
    cx.CursorKind.VAR_DECL: "variable",
    cx.CursorKind.TYPEDEF_DECL: "type_alias",
    cx.CursorKind.TYPE_ALIAS_DECL: "type_alias",
    cx.CursorKind.TYPE_ALIAS_TEMPLATE_DECL: "type_alias",
    cx.CursorKind.ENUM_DECL: "enum",
    cx.CursorKind.ENUM_CONSTANT_DECL: "enum_constant",
}


# =============================================================================
# Name resolver
# =============================================================================


class CppLogicalNameResolver(LogicalNameResolver):
    """Maps ``foo::bar::baz`` (or ``foo.bar.baz``) names to header/source paths.

    :ivar _project_root: project root; resolutions report paths relative to it.
    :ivar _source_roots: ordered include/source directories to probe.
    :ivar _extensions: file extensions to try, in order. Default covers the
        common header + source conventions.
    """

    _DEFAULT_EXTENSIONS: tuple[str, ...] = (".hpp", ".h", ".hxx", ".hh", ".cpp", ".cxx", ".cc", ".c")

    def __init__(
        self,
        project_root: Path,
        source_roots: Sequence[Path] = (),
        extensions: Sequence[str] = (),
    ):
        """:param project_root: directory at whose root paths are reported.
        :param source_roots: directories under which headers and source live;
            defaults to ``(project_root,)``.
        :param extensions: file extensions probed in order (first match wins);
            defaults to :attr:`_DEFAULT_EXTENSIONS`.
        """
        # canonicalize inputs so resolution does not depend on caller CWD
        self._project_root = project_root.resolve()
        resolved_roots = tuple(r.resolve() for r in source_roots)
        self._source_roots: tuple[Path, ...] = resolved_roots if resolved_roots else (self._project_root,)
        self._extensions: tuple[str, ...] = tuple(extensions) if extensions else self._DEFAULT_EXTENSIONS

    def parse(self, raw: str) -> LogicalName:
        # grammar: ``a::b::c`` or ``a.b.c``; components must be valid C++ identifiers
        if not raw:
            raise NameResolutionError(raw, "empty logical name")
        parts = tuple(re.split(r"::|\.", raw))
        for part in parts:
            if not part or not _is_cpp_identifier(part):
                raise NameResolutionError(raw, f"invalid C++ identifier part: {part!r}")
        return LogicalName(parts=parts, raw=raw)

    def resolve(self, name: LogicalName) -> NameResolution:
        # probe each source root for an existing file under each extension
        rel = Path(*name.parts)
        for root in self._source_roots:
            for ext in self._extensions:
                candidate = root / rel.with_suffix(ext)
                if candidate.is_file():
                    return self._resolution_for(candidate, exists=True)
        # synthesize a creation path under the first root with the first extension
        synthetic = self._source_roots[0] / rel.with_suffix(self._extensions[0])
        return self._resolution_for(synthetic, exists=False)

    def _resolution_for(self, absolute: Path, exists: bool) -> NameResolution:
        # enforce that the target sits inside the project root so callers get usable relative paths
        try:
            relative = absolute.relative_to(self._project_root)
        except ValueError as err:
            raise NameResolutionError(
                str(absolute),
                f"resolved path {absolute} escapes project root {self._project_root}",
            ) from err
        return NameResolution(relative_path=str(relative), source_kind="translation_unit", exists=exists)


_CPP_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_cpp_identifier(part: str) -> bool:
    # a C++ identifier is a non-empty leading letter/underscore followed by letters/digits/underscores
    match = _CPP_IDENTIFIER_RE.fullmatch(part)
    return match is not None


# =============================================================================
# Data classes for opaque handles
# =============================================================================


@dataclass(frozen=True)
class _Edit:
    """A pending source-buffer edit anchored at a byte range.

    :ivar offset: byte offset into the source where the edit begins.
    :ivar length: number of original bytes to replace (0 for pure insertion).
    :ivar replacement: the replacement text.
    """

    offset: int
    length: int
    replacement: str


@dataclass(frozen=True)
class _CppTree:
    """Opaque handle for a parsed C++ translation unit.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    :ivar tu: the libclang :class:`TranslationUnit` parsed from ``source``.
    :ivar index: the owning :class:`Index`; held here so the TU's lifetime is
        at least as long as the tree handle's.
    :ivar virtual_filename: the name under which ``source`` was parsed (via
        :attr:`TranslationUnit.unsaved_files`). Reused on re-parse after
        edits so the virtual path stays stable.
    :ivar compile_args: clang flags used for parsing, preserved for re-parse.
    """

    source: str
    tu: cx.TranslationUnit
    index: cx.Index
    virtual_filename: str
    compile_args: tuple[str, ...]


@dataclass(frozen=True)
class _CppSymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    ``walk_symbols`` yields these rather than raw :class:`Cursor` handles
    because cursors are tied to the TU that produced them — re-parse after a
    mutation invalidates every cursor. An offset+length pair survives a
    re-parse: the same byte range is simply re-cursor'd as needed.

    :ivar kind: structural kind name.
    :ivar name_path: Parent/Child path within the translation unit.
    :ivar extent_offset: start byte of the symbol's whole declaration.
    :ivar extent_length: byte length of the symbol's whole declaration.
    :ivar body_range: for compound declarations, the byte range BETWEEN the
        opening and closing braces (exclusive of the braces themselves). Used
        by :meth:`insert_child` to place children inside the body. ``None``
        for leaf declarations that have no body.
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _CppDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _CppPattern(AstPattern):  # type: ignore[misc]
    """A compiled pattern: the parsed pattern TU plus placeholder metadata.

    :ivar source: original pattern string (pre-encoding).
    :ivar encoded_source: the ``$``-sigil-free text that libclang actually parsed.
    :ivar pattern_tu: libclang :class:`TranslationUnit` for the encoded pattern.
    :ivar pattern_cursor_extent: byte extent, within the encoded source, of the
        cursor that represents the user's pattern (after unwrapping the
        synthetic context used to make fragments parseable).
    :ivar placeholders: encoded-identifier → :class:`_Placeholder` lookup.
    :ivar wrapper: which wrapping strategy succeeded, for diagnostics.
    """

    source: str
    encoded_source: str
    pattern_tu: cx.TranslationUnit
    pattern_cursor_extent: tuple[int, int]
    placeholders: Mapping[str, _Placeholder]
    wrapper: str


@dataclass(frozen=True)
class _Placeholder:
    """Metadata for one ``$``-sigil captured inside a pattern.

    :ivar name: the capture name the agent wrote; ``"_"`` is an anonymous wildcard.
    :ivar is_sequence: ``True`` iff the sigil was ``$*name``.
    :ivar encoded: synthetic identifier substituted into the pattern source.
    """

    name: str
    is_sequence: bool
    encoded: str


# =============================================================================
# Pattern encoding and parsing
# =============================================================================

_SIGIL_RE = re.compile(r"\$(?P<seq>\*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*|_)")


def _encode_sigils(src: str) -> tuple[str, dict[str, _Placeholder]]:
    """Rewrite ``$``-sigils to synthetic identifiers libclang can parse."""
    placeholders: dict[str, _Placeholder] = {}
    wildcard_counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal wildcard_counter
        raw_name = match.group("name")
        is_seq = match.group("seq") == "*"
        if raw_name == "_":
            encoded = f"__serena_wild_{wildcard_counter}__"
            wildcard_counter += 1
        else:
            encoded = f"__serena_cap_{raw_name}__"
        placeholders[encoded] = _Placeholder(name=raw_name, is_sequence=is_seq, encoded=encoded)
        return encoded

    return _SIGIL_RE.sub(_replace, src), placeholders


# wrapping strategies for parsing fragment patterns; each returns
# (wrapped_source, extent_within_wrapped) so the caller can locate the actual
# pattern cursor after parsing.
_PROBE_STMT_PREFIX = "void __serena_probe__() {\n"
_PROBE_STMT_SUFFIX = "\n}\n"
_PROBE_EXPR_PREFIX = "auto __serena_probe__ = (\n"
_PROBE_EXPR_SUFFIX = "\n);\n"
_PROBE_MEMBER_PREFIX = "struct __serena_probe__ {\n"
_PROBE_MEMBER_SUFFIX = "\n};\n"


def _try_wrap_top_level(encoded: str) -> tuple[str, tuple[int, int]] | None:
    # top-level: the fragment IS a translation unit. No wrapping.
    return encoded, (0, len(encoded))


def _try_wrap_statement(encoded: str) -> tuple[str, tuple[int, int]]:
    # statement: wrap in a function body so free-standing statements parse.
    wrapped = _PROBE_STMT_PREFIX + encoded + _PROBE_STMT_SUFFIX
    return wrapped, (len(_PROBE_STMT_PREFIX), len(_PROBE_STMT_PREFIX) + len(encoded))


def _try_wrap_expression(encoded: str) -> tuple[str, tuple[int, int]]:
    # expression: wrap in a declarator initializer.
    wrapped = _PROBE_EXPR_PREFIX + encoded + _PROBE_EXPR_SUFFIX
    return wrapped, (len(_PROBE_EXPR_PREFIX), len(_PROBE_EXPR_PREFIX) + len(encoded))


def _try_wrap_member(encoded: str) -> tuple[str, tuple[int, int]]:
    # member: wrap in a struct so member declarations parse in-context.
    wrapped = _PROBE_MEMBER_PREFIX + encoded + _PROBE_MEMBER_SUFFIX
    return wrapped, (len(_PROBE_MEMBER_PREFIX), len(_PROBE_MEMBER_PREFIX) + len(encoded))


# =============================================================================
# Cursor traversal helpers
# =============================================================================


def _cursor_kind_to_structural(kind: cx.CursorKind) -> KindName | None:
    # unknown cursor kinds (EOF, attribute, implicit, ...) are intentionally not surfaced
    return _CURSOR_KIND_TO_STRUCTURAL.get(kind)


def _cursor_spelling(cursor: cx.Cursor) -> str:
    # libclang's spelling is already the identifier (no surrounding source); keep it as-is
    return cursor.spelling or ""


def _in_main_file(cursor: cx.Cursor, main_file_name: str) -> bool:
    # every cursor carries a location; includes bring in foreign cursors we must skip
    loc = cursor.location
    return loc.file is not None and loc.file.name == main_file_name


def _body_range_for_compound(
    cursor: cx.Cursor, source: str
) -> tuple[int, int] | None:
    """Return the byte range inside a compound declaration's braces.

    :param cursor: a cursor whose declaration ends in ``{...}``.
    :param source: the source text the cursor was parsed from.
    :return: ``(start, end)`` where ``start`` is the offset just after ``{``
        and ``end`` the offset of the matching ``}``; ``None`` if the cursor
        has no discernible brace-delimited body in ``source``.
    """
    ext = cursor.extent
    start = ext.start.offset
    end = ext.end.offset
    if end <= start or end > len(source):
        return None
    # find the first '{' inside the extent that is not within a string/comment
    open_brace = _find_opening_brace(source, start, end)
    if open_brace is None:
        return None
    close_brace = _find_matching_brace(source, open_brace, end)
    if close_brace is None:
        return None
    return (open_brace + 1, close_brace)


def _find_opening_brace(source: str, start: int, end: int) -> int | None:
    # walk forward respecting string/char literals; this is a heuristic tuned
    # for declaration headers (no comments inside header syntax)
    i = start
    while i < end:
        ch = source[i]
        if ch == "{":
            return i
        if ch == '"':
            i = _skip_string(source, i, '"', end)
            continue
        if ch == "'":
            i = _skip_string(source, i, "'", end)
            continue
        if ch == "/" and i + 1 < end:
            nxt = source[i + 1]
            if nxt == "/":
                i = _skip_line_comment(source, i, end)
                continue
            if nxt == "*":
                i = _skip_block_comment(source, i, end)
                continue
        i += 1
    return None


def _find_matching_brace(source: str, open_offset: int, end: int) -> int | None:
    # brace-counting scan; respects strings, chars, and comments
    depth = 0
    i = open_offset
    while i < end:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        elif ch == '"':
            i = _skip_string(source, i, '"', end)
            continue
        elif ch == "'":
            i = _skip_string(source, i, "'", end)
            continue
        elif ch == "/" and i + 1 < end:
            nxt = source[i + 1]
            if nxt == "/":
                i = _skip_line_comment(source, i, end)
                continue
            if nxt == "*":
                i = _skip_block_comment(source, i, end)
                continue
        i += 1
    return None


def _skip_string(source: str, start: int, quote: str, end: int) -> int:
    # handle escape sequences; stops at the closing quote
    i = start + 1
    while i < end:
        if source[i] == "\\" and i + 1 < end:
            i += 2
            continue
        if source[i] == quote:
            return i + 1
        i += 1
    return end


def _skip_line_comment(source: str, start: int, end: int) -> int:
    i = source.find("\n", start, end)
    return (i + 1) if i != -1 else end


def _skip_block_comment(source: str, start: int, end: int) -> int:
    i = source.find("*/", start + 2, end)
    return (i + 2) if i != -1 else end


def _walk_named_symbols(
    tree: _CppTree,
) -> Iterator[tuple[str, KindName, _CppSymbolRef]]:
    """Yield all named-symbol triples in the main file of ``tree``."""
    # a depth-first walk tagged with the structural parent path
    def _recurse(cursor: cx.Cursor, prefix: str) -> Iterator[tuple[str, KindName, _CppSymbolRef]]:
        for child in cursor.get_children():
            if not _in_main_file(child, tree.virtual_filename):
                continue
            kind = _cursor_kind_to_structural(child.kind)
            if kind is None:
                # descend into groups we do not surface (e.g. access specifiers wrap members)
                yield from _recurse(child, prefix)
                continue
            name = _cursor_spelling(child)
            if not name and kind != "include":
                yield from _recurse(child, prefix)
                continue
            # include directives carry the header name in displayname
            if kind == "include":
                name = child.displayname or name
            path = f"{prefix}/{name}" if prefix else name
            ext = child.extent
            body_range = _body_range_for_compound(child, tree.source) if kind in _COMPOUND_KINDS else None
            ref = _CppSymbolRef(
                kind=kind,
                name_path=path,
                extent_offset=ext.start.offset,
                extent_length=ext.end.offset - ext.start.offset,
                body_range=body_range,
            )
            yield path, kind, ref
            # recurse into compound declarations that may hold further named symbols
            if kind in _COMPOUND_KINDS:
                yield from _recurse(child, path)

    yield from _recurse(tree.tu.cursor, prefix="")


_COMPOUND_KINDS: frozenset[KindName] = frozenset(
    {"namespace", "class", "struct", "union", "enum"}
)


# =============================================================================
# Edit application and parsing
# =============================================================================


_DEFAULT_COMPILE_ARGS: tuple[str, ...] = ("-std=c++20", "-x", "c++")
_DEFAULT_VIRTUAL_FILENAME = "__serena_source__.cpp"


def _apply_edits(source: str, edits: Sequence[_Edit]) -> str:
    """Apply ``edits`` to ``source`` in reverse-offset order."""
    # earlier-offset edits never shift later ones when applied right-to-left
    out = source
    for edit in sorted(edits, key=lambda e: e.offset, reverse=True):
        out = out[: edit.offset] + edit.replacement + out[edit.offset + edit.length :]
    return out


def _parse_source(
    source: str,
    index: cx.Index,
    virtual_filename: str,
    compile_args: Sequence[str],
    raise_on_fatal: bool,
) -> cx.TranslationUnit:
    """Parse ``source`` into a :class:`TranslationUnit` held by ``index``."""
    # PARSE_DETAILED_PROCESSING_RECORD lets us see INCLUSION_DIRECTIVE cursors
    try:
        tu = index.parse(
            virtual_filename,
            args=list(compile_args),
            unsaved_files=[(virtual_filename, source)],
            options=cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
    except cx.TranslationUnitLoadError as err:
        raise ParseError(language_key="cpp", source_preview=source[:240], detail=str(err)) from None

    if raise_on_fatal:
        # libclang produces a usable AST even for broken sources; the admissibility
        # gate is "libclang returned a TU at all", which the try/except above already
        # enforces. Header-not-found and syntax errors are deliberately tolerated so
        # partial sources (missing include paths, WIP edits) remain parseable.
        pass
    return tu


# =============================================================================
# The backend
# =============================================================================


class CppStructuralLanguage(StructuralLanguage):
    """Structural backend for C and C++ sources, powered by libclang."""

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        compile_args: Sequence[str] = (),
        virtual_filename: str = _DEFAULT_VIRTUAL_FILENAME,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`CppLogicalNameResolver` rooted at the CWD.
        :param compile_args: clang flags for every parse in this backend.
            Defaults to ``('-std=c++20', '-x', 'c++')``. Projects with a
            :file:`compile_commands.json` should route per-file flags through
            a wrapper rather than customize this global default.
        :param virtual_filename: the filename under which in-memory sources are
            parsed. Must match the suffix assumptions of the compile args.
        """
        # one Index per backend instance; cindex recommends reuse across TUs
        self._index = cx.Index.create()
        self._name_resolver = name_resolver or CppLogicalNameResolver(Path.cwd())
        self._compile_args: tuple[str, ...] = tuple(compile_args) if compile_args else _DEFAULT_COMPILE_ARGS
        self._virtual_filename = virtual_filename

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "cpp"

    @property
    def kind_schema(self) -> KindSchema:
        return _CPP_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- parse / serialize -------------------------------------------------

    def parse(self, source: str) -> _CppTree:
        # fatal errors (out of memory, libclang crash) become ParseError
        tu = _parse_source(
            source,
            self._index,
            self._virtual_filename,
            self._compile_args,
            raise_on_fatal=True,
        )
        return _CppTree(
            source=source,
            tu=tu,
            index=self._index,
            virtual_filename=self._virtual_filename,
            compile_args=self._compile_args,
        )

    def serialize(self, tree: Any) -> str:
        # handles carry their source verbatim; no edits are cached on the handle
        if isinstance(tree, _CppTree):
            return tree.source
        if isinstance(tree, _CppDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _CppTree):
            raise TypeError(f"root_kind expects a _CppTree, got {type(tree).__name__}")
        return "translation_unit"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _CppTree):
            raise TypeError(f"walk_symbols expects a _CppTree, got {type(tree).__name__}")
        return list(_walk_named_symbols(tree))

    # ---- declaration -------------------------------------------------------

    def build_declaration(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
        children: Iterable[Any],
    ) -> _CppDeclaration:
        self.kind_schema.get(kind)  # validates the kind name exists in the schema
        children_list = list(children)

        # validate that children belong to this backend
        for child in children_list:
            if not isinstance(child, _CppDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _CppDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "translation_unit":
            # translation_unit is built implicitly via insert_child; reject explicit construction
            raise DeclarationError(kind, "construct translation_unit via empty_source() plus insert_child()")

        if kind == "include":
            statement = _require_str(attributes, "statement", kind)
            if not statement.lstrip().startswith("#include"):
                raise DeclarationError(kind, "include statement must start with '#include'")
            return _CppDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "namespace":
            name = _require_str(attributes, "name", kind)
            body = _optional_str(attributes, "body", kind)
            child_source = _concat_children_source(children_list)
            inner = _join_bodies(body, child_source)
            rendered = f"namespace {name} {{\n{inner}}}\n"
            return _CppDeclaration(kind=kind, source=rendered)

        if kind in {"class", "struct", "union"}:
            name = _require_str(attributes, "name", kind)
            bases = _optional_str_list(attributes, "bases", kind)
            body = _optional_str(attributes, "body", kind)
            template_params = _optional_str_or_none(attributes, "template_parameters", kind)
            child_source = _concat_children_source(children_list)
            inner = _join_bodies(body, child_source)
            keyword = kind  # one of class/struct/union
            base_clause = ""
            if bases:
                base_clause = " : " + ", ".join(bases)
            template_prefix = f"template <{template_params}>\n" if template_params else ""
            rendered = f"{template_prefix}{keyword} {name}{base_clause} {{\n{inner}}};\n"
            return _CppDeclaration(kind=kind, source=rendered)

        if kind in {"function", "method"}:
            name = _require_str(attributes, "name", kind)
            params = _optional_str(attributes, "parameters", kind)
            return_type = _require_str(attributes, "return_type", kind)
            body = _optional_str(attributes, "body", kind)
            qualifiers = _optional_str(attributes, "qualifiers", kind)
            template_params = _optional_str_or_none(attributes, "template_parameters", kind)
            template_prefix = f"template <{template_params}>\n" if template_params else ""
            qual_suffix = f" {qualifiers}" if qualifiers else ""
            rendered = (
                f"{template_prefix}{return_type} {name}({params}){qual_suffix} {{\n"
                f"{_indent_body(body)}"
                f"}}\n"
            )
            return _CppDeclaration(kind=kind, source=rendered)

        if kind == "field":
            name = _require_str(attributes, "name", kind)
            type_ = _require_str(attributes, "type", kind)
            initializer = _optional_str_or_none(attributes, "initializer", kind)
            init_clause = f" = {initializer}" if initializer is not None else ""
            rendered = f"{type_} {name}{init_clause};\n"
            return _CppDeclaration(kind=kind, source=rendered)

        if kind == "variable":
            name = _require_str(attributes, "name", kind)
            type_ = _require_str(attributes, "type", kind)
            initializer = _optional_str_or_none(attributes, "initializer", kind)
            init_clause = f" = {initializer}" if initializer is not None else ""
            rendered = f"{type_} {name}{init_clause};\n"
            return _CppDeclaration(kind=kind, source=rendered)

        if kind == "type_alias":
            statement = _require_str(attributes, "statement", kind)
            stripped = statement.lstrip()
            if not (stripped.startswith("using ") or stripped.startswith("typedef ")):
                raise DeclarationError(kind, "type_alias statement must start with 'using' or 'typedef'")
            return _CppDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "enum":
            name = _require_str(attributes, "name", kind)
            body = _optional_str(attributes, "body", kind)
            child_source = _concat_children_source(children_list)
            inner = _join_bodies(body, child_source)
            rendered = f"enum {name} {{\n{inner}}};\n"
            return _CppDeclaration(kind=kind, source=rendered)

        if kind == "enum_constant":
            name = _require_str(attributes, "name", kind)
            initializer = _optional_str_or_none(attributes, "initializer", kind)
            init_clause = f" = {initializer}" if initializer is not None else ""
            rendered = f"{name}{init_clause},\n"
            return _CppDeclaration(kind=kind, source=rendered)

        raise DeclarationError(kind, f"unsupported kind in C++ backend: {kind!r}")

    # ---- insert / remove ---------------------------------------------------

    def insert_child(
        self, parent: Any, child: Any, anchor: Any | None = None, position: str = "end"
    ) -> _CppTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _CppDeclaration):
            raise TypeError(f"child must be a _CppDeclaration; got {type(child).__name__}")

        tree, parent_ref = self._resolve_insertion_parent(parent)
        offset = self._compute_insertion_offset(tree, parent_ref, anchor, position)
        # newline discipline: ensure a blank-or-newline boundary between adjacent top-level decls
        edit_text = child.source if child.source.endswith("\n") else child.source + "\n"
        edit = _Edit(offset=offset, length=0, replacement=edit_text)
        new_source = _apply_edits(tree.source, [edit])
        return self._reparse(new_source, tree)

    def remove_child(self, parent: Any, child: Any) -> _CppTree:
        tree, _parent_ref = self._resolve_insertion_parent(parent)
        if not isinstance(child, _CppSymbolRef):
            raise TypeError(
                f"child to remove must be a _CppSymbolRef from walk_symbols; got {type(child).__name__}"
            )
        edit = _Edit(offset=child.extent_offset, length=child.extent_length, replacement="")
        new_source = _apply_edits(tree.source, [edit])
        return self._reparse(new_source, tree)

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_CppTree, _CppSymbolRef | None]:
        # parent may be the tree itself (TU-level insertion) or a symbol ref (nested insertion)
        if isinstance(parent, _CppTree):
            return parent, None
        if isinstance(parent, _CppSymbolRef):
            # the tree isn't carried on the ref; for M2, nested insertion requires the caller
            # to also hold the tree and call via insert_child(tree, child, anchor=parent_ref, ...)
            raise TypeError(
                "insert_child on a _CppSymbolRef is not supported; pass the _CppTree and use "
                "anchor=symbol_ref with position='before' or 'after' instead"
            )
        raise TypeError(f"parent must be _CppTree; got {type(parent).__name__}")

    def _compute_insertion_offset(
        self,
        tree: _CppTree,
        parent_ref: _CppSymbolRef | None,
        anchor: Any,
        position: str,
    ) -> int:
        # anchor may be a _CppSymbolRef (nested target) or None
        if anchor is not None and not isinstance(anchor, _CppSymbolRef):
            raise TypeError(f"anchor must be _CppSymbolRef or None; got {type(anchor).__name__}")

        if anchor is None:
            # TU-level start/end when no anchor
            if position == "start":
                return 0
            if position == "end":
                return len(tree.source)
            raise ValueError(f"position {position!r} requires an anchor")

        # nested-into-symbol cases: anchor is a compound symbol whose body_range we insert into
        if position == "start":
            if anchor.body_range is None:
                raise ValueError(f"anchor {anchor.name_path!r} has no body to insert into")
            return anchor.body_range[0]
        if position == "end":
            if anchor.body_range is None:
                raise ValueError(f"anchor {anchor.name_path!r} has no body to insert into")
            return anchor.body_range[1]

        # before / after: adjacent to the anchor declaration
        if position == "before":
            return anchor.extent_offset
        # after
        return anchor.extent_offset + anchor.extent_length

    def _reparse(self, new_source: str, prior_tree: _CppTree) -> _CppTree:
        # preserve index, virtual filename, and compile args across re-parse
        tu = _parse_source(
            new_source,
            prior_tree.index,
            prior_tree.virtual_filename,
            prior_tree.compile_args,
            raise_on_fatal=True,
        )
        return _CppTree(
            source=new_source,
            tu=tu,
            index=prior_tree.index,
            virtual_filename=prior_tree.virtual_filename,
            compile_args=prior_tree.compile_args,
        )

    # ---- pattern matching & rewriting --------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError("parse", "pattern source is empty")
        encoded, placeholders = _encode_sigils(pattern_source)
        # try wrappings in order of specificity
        attempts = (
            ("top_level", *_safe_call(_try_wrap_top_level, encoded)),
            ("statement", *_try_wrap_statement(encoded)),
            ("expression", *_try_wrap_expression(encoded)),
            ("member", *_try_wrap_member(encoded)),
        )
        last_error: Exception | None = None
        for name, wrapped, extent in attempts:
            try:
                tu = _parse_source(
                    wrapped,
                    self._index,
                    f"__serena_pattern_{name}__.cpp",
                    self._compile_args,
                    raise_on_fatal=False,
                )
            except ParseError as err:
                last_error = err
                continue
            if _has_fatal_diagnostics(tu):
                continue
            return _CppPattern(
                source=pattern_source,
                encoded_source=wrapped,
                pattern_tu=tu,
                pattern_cursor_extent=extent,
                placeholders=placeholders,
                wrapper=name,
            )
        raise PatternError("parse", f"no wrapping parsed pattern cleanly: {last_error}")

    def find_matches(
        self, tree: Any, pattern: AstPattern, scope: Any | None = None
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _CppTree):
            raise TypeError(f"tree must be a _CppTree; got {type(tree).__name__}")
        if not isinstance(pattern, _CppPattern):
            raise TypeError(f"pattern must come from this backend's compile_pattern; got {type(pattern).__name__}")
        scope_ref = scope if isinstance(scope, _CppSymbolRef) else None
        return list(self._iter_matches(tree, scope_ref, pattern))

    def _iter_matches(
        self, tree: _CppTree, scope: _CppSymbolRef | None, pattern: _CppPattern
    ) -> Iterator[PatternMatch]:
        # locate the pattern's representative cursor (the cursor whose extent best matches the
        # reserved pattern range inside the encoded source)
        pattern_cursors = list(_cursors_within_range(pattern.pattern_tu, pattern.pattern_cursor_extent))
        if not pattern_cursors:
            return
        # pick the smallest enclosing cursor: the first one whose extent contains the whole pattern
        pat_offset_start, pat_offset_end = pattern.pattern_cursor_extent
        pattern_root = _smallest_enclosing(pattern_cursors, pat_offset_start, pat_offset_end)
        if pattern_root is None:
            return

        symbol_path_map = self._symbol_path_map(tree)
        scope_range: tuple[int, int] | None = None
        if scope is not None:
            scope_range = (scope.extent_offset, scope.extent_offset + scope.extent_length)

        def _visit(cursor: cx.Cursor) -> Iterator[PatternMatch]:
            if not _in_main_file(cursor, tree.virtual_filename):
                # descend through include directives to reach the main file's cursors
                for child in cursor.get_children():
                    yield from _visit(child)
                return
            # respect the optional scope filter
            if scope_range is not None:
                ext = cursor.extent
                if not (ext.start.offset >= scope_range[0] and ext.end.offset <= scope_range[1]):
                    for child in cursor.get_children():
                        yield from _visit(child)
                    return
            bindings: dict[str, Any] = {}
            if _cursor_match(cursor, pattern_root, pattern.placeholders, bindings):
                ext = cursor.extent
                match_source = tree.source[ext.start.offset : ext.end.offset]
                yield PatternMatch(
                    node=_CppSymbolRef(
                        kind="match",  # patterns may land on non-symbol nodes
                        name_path=match_source,
                        extent_offset=ext.start.offset,
                        extent_length=ext.end.offset - ext.start.offset,
                        body_range=None,
                    ),
                    bindings={k: _freeze_binding(v, tree.source) for k, v in bindings.items()},
                    symbol_path=symbol_path_map.get((ext.start.offset, ext.end.offset)),
                )
            for child in cursor.get_children():
                yield from _visit(child)

        yield from _visit(tree.tu.cursor)

    def _symbol_path_map(self, tree: _CppTree) -> dict[tuple[int, int], str]:
        # map every named-symbol extent to its structural path for match diagnostics
        mapping: dict[tuple[int, int], str] = {}
        for path, _kind, ref in _walk_named_symbols(tree):
            key = (ref.extent_offset, ref.extent_offset + ref.extent_length)
            mapping[key] = path
        return mapping

    def render_replacement(
        self, replacement_source: str, bindings: Mapping[str, Any]
    ) -> _CppDeclaration:
        # substitute $name tokens with the captured source strings; the result is a
        # rendered text fragment the caller can feed to apply_replacement
        encoded, placeholders = _encode_sigils(replacement_source)
        for placeholder in placeholders.values():
            if placeholder.name == "_":
                raise PatternError(
                    "parse",
                    f"replacement source contains wildcard {placeholder.encoded!r} which cannot be filled",
                )
            if placeholder.name not in bindings:
                raise PatternError(
                    "parse",
                    f"replacement references capture {placeholder.name!r} with no binding",
                )
        rendered = encoded
        for encoded_name, placeholder in placeholders.items():
            replacement_text = bindings[placeholder.name]
            if not isinstance(replacement_text, str):
                raise PatternError(
                    "parse",
                    f"capture {placeholder.name!r} is not a text binding; got {type(replacement_text).__name__}",
                )
            rendered = rendered.replace(encoded_name, replacement_text)
        return _CppDeclaration(kind="replacement", source=rendered)

    def apply_replacement(
        self, tree: Any, match: PatternMatch, replacement: Any
    ) -> _CppTree:
        if not isinstance(tree, _CppTree):
            raise TypeError(f"tree must be a _CppTree; got {type(tree).__name__}")
        if not isinstance(match.node, _CppSymbolRef):
            raise TypeError(f"match.node must be a _CppSymbolRef; got {type(match.node).__name__}")
        if not isinstance(replacement, _CppDeclaration):
            raise TypeError(f"replacement must be a _CppDeclaration; got {type(replacement).__name__}")
        edit = _Edit(
            offset=match.node.extent_offset,
            length=match.node.extent_length,
            replacement=replacement.source,
        )
        new_source = _apply_edits(tree.source, [edit])
        return self._reparse(new_source, tree)

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _CppTree:
        if source_kind != "translation_unit":
            raise DeclarationError(source_kind, f"cpp has no source kind {source_kind!r}")
        # empty string parses as a trivially valid TU; round-trips to empty
        return self.parse("")


# =============================================================================
# Attribute helpers
# =============================================================================


def _require_str(attrs: Mapping[str, Any], name: str, kind: KindName) -> str:
    # missing required attributes raise DeclarationError with a precise reference
    if name not in attrs or attrs[name] is None:
        raise DeclarationError(kind, f"attribute {name!r} is required")
    value = attrs[name]
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be str, got {type(value).__name__}")
    return value


def _optional_str(attrs: Mapping[str, Any], name: str, kind: KindName) -> str:
    # missing optional strings default to empty
    value = attrs.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be str, got {type(value).__name__}")
    return value


def _optional_str_or_none(attrs: Mapping[str, Any], name: str, kind: KindName) -> str | None:
    # distinct from _optional_str for attributes where absence must remain absent
    value = attrs.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be str or None, got {type(value).__name__}")
    return value


def _optional_str_list(attrs: Mapping[str, Any], name: str, kind: KindName) -> tuple[str, ...]:
    value = attrs.get(name)
    if value is None:
        return ()
    if isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be list[str], not a single str")
    if not isinstance(value, (list, tuple)):
        raise DeclarationError(kind, f"attribute {name!r} must be list[str], got {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise DeclarationError(kind, f"attribute {name!r} item {i} must be str, got {type(item).__name__}")
        out.append(item)
    return tuple(out)


# =============================================================================
# Source rendering helpers
# =============================================================================


def _ensure_trailing_newline(text: str) -> str:
    # included/type-alias statements always end on a newline so adjacent lines compose cleanly
    return text if text.endswith("\n") else text + "\n"


def _concat_children_source(children: Sequence[_CppDeclaration]) -> str:
    # children already end with a newline per build_declaration's rendering rules
    return "".join(child.source for child in children)


def _join_bodies(raw_body: str, child_source: str) -> str:
    # compose the body text for compound declarations; always end with a trailing newline
    parts: list[str] = []
    if raw_body:
        parts.append(raw_body if raw_body.endswith("\n") else raw_body + "\n")
    if child_source:
        parts.append(child_source)
    joined = "".join(parts)
    return joined if joined.endswith("\n") else joined + "\n"


def _indent_body(raw_body: str) -> str:
    # leave hand-crafted indentation alone; otherwise indent each line one level
    if not raw_body:
        return ""
    if raw_body.startswith(" ") or raw_body.startswith("\t"):
        return raw_body if raw_body.endswith("\n") else raw_body + "\n"
    indented = "\n".join("    " + line if line.strip() else line for line in raw_body.splitlines())
    return indented + "\n"


# =============================================================================
# Pattern helpers
# =============================================================================


def _safe_call(fn: Any, *args: Any) -> tuple[Any, ...]:
    # _try_wrap_top_level returns a tuple; this helper just forwards it unpacked for the attempts list
    result = fn(*args)
    return result if result is not None else ("", (0, 0))


def _has_fatal_diagnostics(tu: cx.TranslationUnit) -> bool:
    return any(d.severity >= cx.Diagnostic.Fatal for d in tu.diagnostics)


def _cursors_within_range(
    tu: cx.TranslationUnit, extent: tuple[int, int]
) -> Iterator[cx.Cursor]:
    # yield every cursor whose extent overlaps the given byte range
    start, end = extent
    target_file: str | None = None

    def _recurse(cursor: cx.Cursor) -> Iterator[cx.Cursor]:
        nonlocal target_file
        loc = cursor.location
        # rough filter: skip cursors from other files; the pattern's main file is the first one seen
        file_name = loc.file.name if loc.file else None
        if file_name is not None and target_file is None:
            target_file = file_name
        if file_name is not None and file_name != target_file:
            return
        ext = cursor.extent
        if ext.start.offset < end and ext.end.offset > start:
            yield cursor
        for child in cursor.get_children():
            yield from _recurse(child)

    yield from _recurse(tu.cursor)


def _smallest_enclosing(
    cursors: Sequence[cx.Cursor], start: int, end: int
) -> cx.Cursor | None:
    # smallest cursor whose extent encloses [start, end)
    best: cx.Cursor | None = None
    best_size: int | None = None
    for cursor in cursors:
        ext = cursor.extent
        if ext.start.offset <= start and ext.end.offset >= end:
            size = ext.end.offset - ext.start.offset
            if best_size is None or size < best_size:
                best = cursor
                best_size = size
    return best


def _cursor_match(
    target: cx.Cursor,
    pattern: cx.Cursor,
    placeholders: Mapping[str, _Placeholder],
    bindings: dict[str, Any],
) -> bool:
    """Structurally compare a target cursor against a pattern cursor.

    A placeholder appears as a ``DECL_REF_EXPR`` or ``UNEXPOSED_DECL`` whose
    spelling is the encoded identifier. We treat any such node as a wildcard
    and record the captured source range.
    """
    pat_spell = pattern.spelling or ""
    info = placeholders.get(pat_spell)
    if info is not None:
        # a placeholder matches any target subtree
        if info.is_sequence:
            # sequence placeholders only meaningful in child-list position; single-node here
            return False
        if info.name != "_":
            bindings[info.name] = target
        return True

    # same cursor kind
    if target.kind != pattern.kind:
        return False

    # same spelling for cursors that are identified by name
    tgt_spell = target.spelling or ""
    if pat_spell and pat_spell != tgt_spell:
        return False

    # recursive structural compare across child cursors
    target_children = list(target.get_children())
    pattern_children = list(pattern.get_children())
    if len(target_children) != len(pattern_children):
        return False
    for tgt_child, pat_child in zip(target_children, pattern_children):
        if not _cursor_match(tgt_child, pat_child, placeholders, bindings):
            return False
    return True


def _freeze_binding(value: Any, source: str) -> str:
    # convert a captured cursor into its literal source text (bindings are strings at the API boundary)
    if isinstance(value, cx.Cursor):
        ext = value.extent
        return source[ext.start.offset : ext.end.offset]
    return str(value)
