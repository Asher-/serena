"""Python :class:`StructuralLanguage` backend using libcst.

libcst is the only mainstream Python parser that preserves enough formatting
metadata for byte-identical round-trip (``ast`` normalizes whitespace and
drops comments; ``tree-sitter`` does not reproduce every byte). This backend
therefore uses :mod:`libcst` throughout.

The module provides three public entities:

* :class:`PythonStructuralLanguage` — the concrete
  :class:`~solidlsp.structural.base.StructuralLanguage` implementation.
* :class:`PythonLogicalNameResolver` — dotted-path resolver against a set of
  source roots.
* :func:`python_kind_schema` — factory for the Python kind vocabulary; exposed
  for inspection and tests.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import libcst as cst

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

# attribute specs reused across kinds
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
    description="raw Python source for the body, as it would appear between a suite colon and the next dedent",
)
_ATTR_PARAMS = AttributeSpec(
    name="parameters",
    type_hint="str",
    required=False,
    description="raw parameter-list source, e.g. 'self, x: int, *, y=0'",
)
_ATTR_RETURN = AttributeSpec(
    name="return_annotation",
    type_hint="str | None",
    required=False,
    description="raw return-annotation source (without the '->' prefix)",
)
_ATTR_BASES = AttributeSpec(
    name="bases",
    type_hint="list[str]",
    required=False,
    description="raw source of each base expression (e.g. 'Protocol', 'Generic[T]')",
)
_ATTR_IS_ASYNC = AttributeSpec(
    name="is_async",
    type_hint="bool",
    required=False,
    description="whether this is an 'async def'",
)
_ATTR_STATEMENT = AttributeSpec(
    name="statement",
    type_hint="str",
    required=True,
    description="full source of the statement, exactly as it should appear",
)
_ATTR_EXPRESSION = AttributeSpec(
    name="expression",
    type_hint="str",
    required=True,
    description="full source of the expression (without the leading '@' for decorators)",
)
_ATTR_TARGETS = AttributeSpec(
    name="targets",
    type_hint="list[str]",
    required=True,
    description="raw target expressions (usually a single identifier)",
)
_ATTR_VALUE = AttributeSpec(
    name="value",
    type_hint="str",
    required=True,
    description="raw source of the right-hand side expression",
)
_ATTR_ANNOTATION = AttributeSpec(
    name="annotation",
    type_hint="str | None",
    required=False,
    description="raw source of the type annotation (omit to produce a bare assignment)",
)


def python_kind_schema() -> KindSchema:
    """Return the Python structural kind vocabulary.

    Factored as a function so the schema can be rebuilt for tests; the module
    also exposes a cached instance as :data:`_PYTHON_KIND_SCHEMA`.
    """
    # block: declarations that may act as source roots
    module = KindSpec(
        name="module",
        description="A Python source file's top level.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset({"import", "class", "function", "assignment"}),
    )

    # block: leaf-ish top-level elements
    imp = KindSpec(
        name="import",
        description="An 'import' or 'from ... import ...' statement.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"module"}),
        allowed_child_kinds=frozenset(),
    )
    assign = KindSpec(
        name="assignment",
        description="An assignment statement (optionally annotated).",
        attributes=(_ATTR_TARGETS, _ATTR_VALUE, _ATTR_ANNOTATION),
        allowed_parent_kinds=frozenset({"module", "class", "function", "method"}),
        allowed_child_kinds=frozenset(),
    )
    deco = KindSpec(
        name="decorator",
        description="A decorator attached to a class, function, or method.",
        attributes=(_ATTR_EXPRESSION,),
        allowed_parent_kinds=frozenset({"class", "function", "method"}),
        allowed_child_kinds=frozenset(),
    )

    # block: compound declarations
    cls = KindSpec(
        name="class",
        description="A class definition.",
        attributes=(_ATTR_NAME, _ATTR_BASES, _ATTR_BODY),
        allowed_parent_kinds=frozenset({"module", "class", "function", "method"}),
        allowed_child_kinds=frozenset({"class", "function", "method", "assignment", "decorator"}),
    )
    func = KindSpec(
        name="function",
        description="A module- or nested-level function definition.",
        attributes=(_ATTR_NAME, _ATTR_PARAMS, _ATTR_RETURN, _ATTR_BODY, _ATTR_IS_ASYNC),
        allowed_parent_kinds=frozenset({"module", "function", "method"}),
        allowed_child_kinds=frozenset({"function", "class", "assignment", "decorator"}),
    )
    method = KindSpec(
        name="method",
        description="A method definition (a function appearing directly inside a class).",
        attributes=(_ATTR_NAME, _ATTR_PARAMS, _ATTR_RETURN, _ATTR_BODY, _ATTR_IS_ASYNC),
        allowed_parent_kinds=frozenset({"class"}),
        allowed_child_kinds=frozenset({"function", "class", "assignment", "decorator"}),
    )

    return KindSchema(
        language_key="python",
        source_kinds=frozenset({"module"}),
        kinds={
            "module": module,
            "import": imp,
            "assignment": assign,
            "decorator": deco,
            "class": cls,
            "function": func,
            "method": method,
        },
    )


_PYTHON_KIND_SCHEMA = python_kind_schema()


# =============================================================================
# Name resolver
# =============================================================================


class PythonLogicalNameResolver(LogicalNameResolver):
    """Maps dotted Python identifiers to module paths under configured source roots.

    :ivar _project_root: project root (absolute). Resolutions are reported
        as paths relative to this root.
    :ivar _source_roots: ordered source roots (absolute). Lookup probes each in
        turn; synthesized paths for not-yet-created modules land under the first.
    """

    def __init__(self, project_root: Path, source_roots: Sequence[Path] = ()):
        """:param project_root: directory at whose root paths are reported.
        :param source_roots: directories under which Python modules live;
            defaults to ``(project_root,)`` when empty.
        """
        # canonicalize inputs so resolution does not depend on caller CWD
        self._project_root = project_root.resolve()
        resolved = tuple(r.resolve() for r in source_roots)
        self._source_roots: tuple[Path, ...] = resolved if resolved else (self._project_root,)

    def parse(self, raw: str) -> LogicalName:
        # grammar: one or more dot-separated identifiers
        if not raw:
            raise NameResolutionError(raw, "empty logical name")
        parts = raw.split(".")
        for part in parts:
            if not part.isidentifier():
                raise NameResolutionError(raw, f"invalid Python identifier part: {part!r}")
        return LogicalName(parts=tuple(parts), raw=raw)

    def resolve(self, name: LogicalName) -> NameResolution:
        # probe each source root for an existing module or package
        rel = Path(*name.parts)
        for root in self._source_roots:
            module_file = root / rel.with_suffix(".py")
            if module_file.is_file():
                return self._resolution_for(module_file, exists=True)
            package_init = root / rel / "__init__.py"
            if package_init.is_file():
                return self._resolution_for(package_init, exists=True)

        # not found; synthesize a creation location under the first root
        synthetic = self._source_roots[0] / rel.with_suffix(".py")
        return self._resolution_for(synthetic, exists=False)

    def _resolution_for(self, absolute: Path, exists: bool) -> NameResolution:
        # enforce that the target is inside the project so callers get usable relative paths
        try:
            relative = absolute.relative_to(self._project_root)
        except ValueError as err:
            raise NameResolutionError(
                str(absolute),
                f"resolved path {absolute} escapes project root {self._project_root}",
            ) from err
        return NameResolution(relative_path=str(relative), source_kind="module", exists=exists)


# =============================================================================
# Pattern support
# =============================================================================

# sigils: $name captures a single node under 'name'; $_ is an unnamed wildcard;
# $*name captures a sequence (only meaningful in statement/argument position).
_SIGIL_RE = re.compile(r"\$(?P<seq>\*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*|_)")


@dataclass(frozen=True)
class _Placeholder:
    """Metadata for one $-sigil captured inside a pattern.

    :ivar name: capture name as the agent wrote it. ``"_"`` denotes an
        anonymous wildcard (no binding is produced).
    :ivar is_sequence: ``True`` if the sigil was ``$*name`` — matches any number
        of adjacent items in a sequence position.
    :ivar encoded: synthetic identifier substituted into the pattern source so
        it remains syntactically valid Python.
    """

    name: str
    is_sequence: bool
    encoded: str


@dataclass(frozen=True)
class _PythonPattern(AstPattern):  # type: ignore[misc]
    """Compiled Python pattern: a libcst node plus placeholder metadata.

    :ivar source: original pattern string (pre-encoding), used for diagnostics.
    :ivar root: parsed libcst node — :class:`cst.BaseExpression`,
        :class:`cst.BaseStatement`, or :class:`cst.Module` depending on what
        the source parsed as.
    :ivar placeholders: encoded-identifier → :class:`_Placeholder` lookup.
    """

    source: str
    root: cst.CSTNode
    placeholders: Mapping[str, _Placeholder]


def _encode_sigils(src: str) -> tuple[str, dict[str, _Placeholder]]:
    """Rewrite $-sigils to synthetic identifiers Python can parse."""
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


def _parse_pattern_fragment(encoded_src: str) -> cst.CSTNode:
    """Parse the encoded pattern source as expression-then-statement-then-module."""
    # try expression first (the common case for patterns like ``foo($x)``)
    try:
        return cst.parse_expression(encoded_src)
    except cst.ParserSyntaxError:
        pass
    # try a single statement
    try:
        return cst.parse_statement(encoded_src)
    except cst.ParserSyntaxError:
        pass
    # fall back to a full module (e.g. multi-line pattern)
    try:
        return cst.parse_module(encoded_src)
    except cst.ParserSyntaxError as err:
        raise PatternError("parse", f"pattern is not a valid Python fragment: {err.message}") from None


# fields libcst uses to record formatting that patterns should ignore
_WHITESPACE_FIELD_PREFIXES = ("whitespace_", "leading_", "trailing_")
_WHITESPACE_FIELD_EXACT = frozenset(
    {
        "header",
        "footer",
        "indent",
        "newline",
        "semicolon",
        "comma",
        "lpar",
        "rpar",
        "lbracket",
        "rbracket",
        "lbrace",
        "rbrace",
        "comments",
    }
)


def _is_ignored_field(field_name: str) -> bool:
    # libcst keeps every whitespace artefact on the node; matches should ignore them
    if field_name in _WHITESPACE_FIELD_EXACT:
        return True
    return any(field_name.startswith(prefix) for prefix in _WHITESPACE_FIELD_PREFIXES) or field_name.endswith("_whitespace")


def _compare_fields(node: cst.CSTNode) -> tuple[str, ...]:
    # libcst nodes are attrs-dataclasses; iterate declared fields, skipping whitespace artefacts
    return tuple(f.name for f in dataclasses.fields(node) if not _is_ignored_field(f.name))


def _match_node(target: Any, pattern: Any, placeholders: Mapping[str, _Placeholder], bindings: dict[str, Any]) -> bool:
    """Structurally match ``target`` against ``pattern``; populate ``bindings`` on success."""
    # placeholder single-node capture or wildcard
    if isinstance(pattern, cst.Name) and pattern.value in placeholders:
        info = placeholders[pattern.value]
        if info.is_sequence:
            return False  # sequence placeholders only meaningful inside a sequence
        if info.name != "_":
            bindings[info.name] = target
        return True

    # non-CST literals compared by equality
    if not isinstance(pattern, cst.CSTNode):
        return target == pattern

    if type(target) is not type(pattern):
        return False

    # recurse field by field, ignoring whitespace artefacts
    for field_name in _compare_fields(pattern):
        pv = getattr(pattern, field_name)
        tv = getattr(target, field_name)
        if isinstance(pv, (list, tuple)):
            if not isinstance(tv, (list, tuple)) or not _match_sequence(tv, pv, placeholders, bindings):
                return False
        elif isinstance(pv, cst.CSTNode):
            if not isinstance(tv, cst.CSTNode) or not _match_node(tv, pv, placeholders, bindings):
                return False
        elif pv is None:
            if tv is not None:
                return False
        else:
            if pv != tv:
                return False
    return True


def _match_sequence(
    target_seq: Sequence[Any],
    pattern_seq: Sequence[Any],
    placeholders: Mapping[str, _Placeholder],
    bindings: dict[str, Any],
) -> bool:
    """Match a sequence, honouring a single ``$*name`` sequence placeholder."""
    # locate a sequence placeholder, if any
    seq_placeholder_index: int | None = None
    seq_placeholder_name: str | None = None
    for i, p in enumerate(pattern_seq):
        maybe_name = _sequence_placeholder_name(p, placeholders)
        if maybe_name is not None:
            if seq_placeholder_index is not None:
                # more than one sequence placeholder is rejected at compile-like stage
                return False
            seq_placeholder_index = i
            seq_placeholder_name = maybe_name

    # no sequence placeholder: lengths must match and items align
    if seq_placeholder_index is None:
        if len(target_seq) != len(pattern_seq):
            return False
        for pv, tv in zip(pattern_seq, target_seq, strict=False):
            if not _match_node(tv, pv, placeholders, bindings):
                return False
        return True

    # with a sequence placeholder: split pattern around it and greedy-match the middle
    prefix = pattern_seq[:seq_placeholder_index]
    suffix = pattern_seq[seq_placeholder_index + 1 :]
    if len(target_seq) < len(prefix) + len(suffix):
        return False
    prefix_target = target_seq[: len(prefix)]
    suffix_target = target_seq[len(target_seq) - len(suffix) :] if suffix else []
    middle_target = target_seq[len(prefix) : len(target_seq) - len(suffix)]
    for pv, tv in zip(prefix, prefix_target, strict=False):
        if not _match_node(tv, pv, placeholders, bindings):
            return False
    for pv, tv in zip(suffix, suffix_target, strict=False):
        if not _match_node(tv, pv, placeholders, bindings):
            return False
    # seq_placeholder_name is non-None here because seq_placeholder_index is non-None
    assert seq_placeholder_name is not None
    if seq_placeholder_name != "_":
        bindings[seq_placeholder_name] = tuple(middle_target)
    return True


def _sequence_placeholder_name(pattern_item: Any, placeholders: Mapping[str, _Placeholder]) -> str | None:
    """Return the capture name if ``pattern_item`` is a ``$*name`` placeholder."""
    # placeholder can appear as a bare Name or wrapped (e.g. Arg(value=Name), SimpleStatementLine(body=[Expr(Name)]))
    if isinstance(pattern_item, cst.Name) and pattern_item.value in placeholders:
        info = placeholders[pattern_item.value]
        return info.name if info.is_sequence else None
    # peek through common wrappers that libcst inserts around bare names in sequences
    for wrapper_field in ("value", "body"):
        inner = getattr(pattern_item, wrapper_field, None)
        if inner is None:
            continue
        if isinstance(inner, (list, tuple)):
            if len(inner) == 1:
                nested = _sequence_placeholder_name(inner[0], placeholders)
                if nested is not None:
                    return nested
        else:
            nested = _sequence_placeholder_name(inner, placeholders)
            if nested is not None:
                return nested
    return None


# =============================================================================
# Symbol walk
# =============================================================================

_SYMBOL_KINDS = {
    cst.ClassDef: "class",
    cst.FunctionDef: "function",
    cst.Import: "import",
    cst.ImportFrom: "import",
    cst.Assign: "assignment",
    cst.AnnAssign: "assignment",
}


def _symbol_name(node: cst.CSTNode) -> str | None:
    """Return the agent-visible name for a symbol node, or ``None`` if the node is unnamed."""
    if isinstance(node, (cst.ClassDef, cst.FunctionDef)):
        return node.name.value
    if isinstance(node, cst.Import):
        # report the first alias as the name (Serena surfaces imports as flat statements)
        return node.names[0].evaluated_name if node.names else None
    if isinstance(node, cst.ImportFrom):
        # 'from X import Y, Z' → report 'Y' as the primary symbol
        if isinstance(node.names, cst.ImportStar):
            return "*"
        return node.names[0].evaluated_name if node.names else None
    if isinstance(node, (cst.Assign, cst.AnnAssign)):
        target = node.target if isinstance(node, cst.AnnAssign) else (node.targets[0].target if node.targets else None)
        if isinstance(target, cst.Name):
            return target.value
        return None
    return None


def _iter_body_statements(node: cst.CSTNode) -> Iterator[cst.CSTNode]:
    """Yield the body statements of ``node`` (modules, classes, functions)."""
    # Module stores statements directly; ClassDef/FunctionDef wrap them in IndentedBlock
    if isinstance(node, cst.Module):
        yield from node.body
    elif isinstance(node, (cst.ClassDef, cst.FunctionDef)):
        body = node.body
        if isinstance(body, cst.IndentedBlock):
            yield from body.body


def _unwrap_statement(stmt: cst.CSTNode) -> cst.CSTNode:
    """Unwrap :class:`SimpleStatementLine` to get the inner statement."""
    if isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
        return stmt.body[0]
    return stmt


def _walk_named_symbols(
    parent: cst.CSTNode, prefix: str, inside_class: bool
) -> Iterator[tuple[str, KindName, cst.CSTNode]]:
    """Recursively yield named-symbol triples under ``parent``."""
    for raw in _iter_body_statements(parent):
        stmt = _unwrap_statement(raw)
        kind_cls = type(stmt)
        if kind_cls not in _SYMBOL_KINDS:
            continue
        base_kind = _SYMBOL_KINDS[kind_cls]
        kind: KindName = "method" if base_kind == "function" and inside_class else base_kind
        name = _symbol_name(stmt)
        if name is None:
            continue
        path = f"{prefix}/{name}" if prefix else name
        # the outer surface yields the SimpleStatementLine for imports/assignments
        # so downstream mutations see the whole statement line
        yield path, kind, raw
        # recurse into bodies that may contain more named symbols
        if isinstance(stmt, cst.ClassDef):
            yield from _walk_named_symbols(stmt, path, inside_class=True)
        elif isinstance(stmt, cst.FunctionDef):
            yield from _walk_named_symbols(stmt, path, inside_class=False)


# =============================================================================
# Construction / mutation helpers
# =============================================================================


def _parse_single_statement(source: str, diagnostic: str) -> cst.BaseStatement:
    """Parse ``source`` and return the single top-level statement it contains."""
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as err:
        raise DeclarationError(diagnostic, f"generated source is not valid Python: {err.message}\n--- source ---\n{source}") from None
    non_empty = [stmt for stmt in module.body if not isinstance(stmt, cst.EmptyLine)]
    if len(non_empty) != 1:
        raise DeclarationError(
            diagnostic,
            f"generated source produced {len(non_empty)} top-level statements; expected 1",
        )
    return non_empty[0]


def _render_class_source(name: str, bases: Sequence[str], body: str) -> str:
    # render as raw text so libcst's own parser handles all the whitespace invariants
    header = f"class {name}"
    if bases:
        header += "(" + ", ".join(bases) + ")"
    header += ":"
    body_text = body or "    pass"
    if not body_text.startswith(" "):
        body_text = "    " + body_text
    return header + "\n" + body_text + ("\n" if not body_text.endswith("\n") else "")


def _render_function_source(
    name: str, parameters: str, return_annotation: str | None, body: str, is_async: bool
) -> str:
    prefix = "async def " if is_async else "def "
    header = f"{prefix}{name}({parameters})"
    if return_annotation:
        header += f" -> {return_annotation}"
    header += ":"
    body_text = body or "    pass"
    if not body_text.startswith(" "):
        body_text = "    " + body_text
    return header + "\n" + body_text + ("\n" if not body_text.endswith("\n") else "")


def _render_assignment_source(targets: Sequence[str], value: str, annotation: str | None) -> str:
    if not targets:
        raise DeclarationError("assignment", "at least one target is required")
    if annotation is not None:
        if len(targets) != 1:
            raise DeclarationError("assignment", "annotated assignments support exactly one target")
        return f"{targets[0]}: {annotation} = {value}\n"
    return " = ".join(list(targets) + [value]) + "\n"


def _render_decorator_source(expression: str) -> str:
    # libcst decorators live on the ClassDef/FunctionDef, but we expose them as their own kind
    # that the agent inserts under a parent; the parent's insert_child is responsible for
    # promoting them onto the correct field.
    return f"@{expression}\ndef __serena_placeholder__():\n    pass\n"


def _coerce_bool(value: Any, attribute: str, kind: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise DeclarationError(kind, f"attribute {attribute!r} must be bool, got {type(value).__name__}")
    return value


def _coerce_str(value: Any, attribute: str, kind: str, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise DeclarationError(kind, f"attribute {attribute!r} is required")
        return ""
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {attribute!r} must be str, got {type(value).__name__}")
    return value


def _coerce_str_list(value: Any, attribute: str, kind: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise DeclarationError(kind, f"attribute {attribute!r} must be a list of str, not a single str")
    if not isinstance(value, (list, tuple)):
        raise DeclarationError(kind, f"attribute {attribute!r} must be list[str], got {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise DeclarationError(kind, f"attribute {attribute!r} item {i} must be str, got {type(item).__name__}")
        out.append(item)
    return tuple(out)


# =============================================================================
# The backend itself
# =============================================================================


class PythonStructuralLanguage(StructuralLanguage):
    """Structural backend for Python, powered by libcst."""

    def __init__(self, name_resolver: LogicalNameResolver | None = None):
        """:param name_resolver: the resolver to expose via :attr:`name_resolver`.
        When omitted, a resolver rooted at the current working directory is
        used. Callers with a :class:`Project` in hand should pass one
        constructed from ``project.project_root``.
        """
        self._name_resolver = name_resolver or PythonLogicalNameResolver(Path.cwd())

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "python"

    @property
    def kind_schema(self) -> KindSchema:
        return _PYTHON_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- parse / serialize -------------------------------------------------

    def parse(self, source: str) -> cst.Module:
        # defer to libcst; convert its parser error to the structural one
        try:
            return cst.parse_module(source)
        except cst.ParserSyntaxError as err:
            preview = source[: err.raw_column + 120] if hasattr(err, "raw_column") else source[:240]
            raise ParseError(language_key="python", source_preview=preview, detail=err.message) from None

    def serialize(self, tree: Any) -> str:
        # libcst guarantees round-trip via tree.code for parsed modules
        if isinstance(tree, cst.Module):
            return tree.code
        # for non-module nodes (pattern fragments, rendered replacements), wrap in a module
        # to access the shared code-generation machinery
        if isinstance(tree, cst.BaseStatement) or isinstance(tree, cst.BaseSmallStatement):
            return cst.Module(body=(tree if isinstance(tree, cst.BaseStatement) else cst.SimpleStatementLine(body=[tree]),)).code  # type: ignore[arg-type]
        if isinstance(tree, cst.CSTNode):
            # fall back to the metadata-agnostic printer
            return cst.Module(body=()).code_for_node(tree)
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, cst.Module):
            raise TypeError(f"root_kind expects a Module, got {type(tree).__name__}")
        return "module"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, cst.Module):
            raise TypeError(f"walk_symbols expects a Module, got {type(tree).__name__}")
        return list(_walk_named_symbols(tree, prefix="", inside_class=False))

    # ---- declaration -------------------------------------------------------

    def build_declaration(
        self, kind: KindName, attributes: Mapping[str, Any], children: Iterable[Any]
    ) -> cst.CSTNode:
        # dispatch per kind; children is consumed for decorator attachment below
        self.kind_schema.get(kind)  # raises if unknown
        child_list = list(children)

        if kind == "module":
            # empty module; ignore attributes/children (populate via insert_child)
            return cst.Module(body=())

        if kind == "import":
            statement = _coerce_str(attributes.get("statement"), "statement", kind)
            parsed = _parse_single_statement(statement, diagnostic=kind)
            if not (isinstance(parsed, cst.SimpleStatementLine) and parsed.body and isinstance(parsed.body[0], (cst.Import, cst.ImportFrom))):
                raise DeclarationError(kind, "statement must be a single 'import' or 'from ... import ...' line")
            return parsed

        if kind == "assignment":
            targets = _coerce_str_list(attributes.get("targets"), "targets", kind)
            value = _coerce_str(attributes.get("value"), "value", kind)
            annotation = attributes.get("annotation")
            if annotation is not None and not isinstance(annotation, str):
                raise DeclarationError(kind, "attribute 'annotation' must be str or None")
            source = _render_assignment_source(targets, value, annotation)
            return _parse_single_statement(source, diagnostic=kind)

        if kind == "decorator":
            expression = _coerce_str(attributes.get("expression"), "expression", kind)
            # parse through a placeholder function to get a well-formed Decorator node
            wrapper = _parse_single_statement(_render_decorator_source(expression), diagnostic=kind)
            if not isinstance(wrapper, cst.FunctionDef) or not wrapper.decorators:
                raise DeclarationError(kind, "decorator expression failed to attach")
            return wrapper.decorators[0]

        if kind in {"class", "function", "method"}:
            # decorators declared as children get promoted onto the new compound node
            decorator_children = [c for c in child_list if isinstance(c, cst.Decorator)]
            if kind == "class":
                name = _coerce_str(attributes.get("name"), "name", kind)
                bases = _coerce_str_list(attributes.get("bases"), "bases", kind)
                body = _coerce_str(attributes.get("body"), "body", kind, required=False)
                source = _render_class_source(name, bases, body)
                node = _parse_single_statement(source, diagnostic=kind)
                if not isinstance(node, cst.ClassDef):
                    raise DeclarationError(kind, "rendered source did not produce a ClassDef")
                if decorator_children:
                    node = node.with_changes(decorators=tuple(decorator_children) + tuple(node.decorators))
                return node
            # function / method share the same rendering
            name = _coerce_str(attributes.get("name"), "name", kind)
            params = _coerce_str(attributes.get("parameters"), "parameters", kind, required=False) or (
                "self" if kind == "method" else ""
            )
            return_annotation = attributes.get("return_annotation")
            if return_annotation is not None and not isinstance(return_annotation, str):
                raise DeclarationError(kind, "attribute 'return_annotation' must be str or None")
            body = _coerce_str(attributes.get("body"), "body", kind, required=False)
            is_async = _coerce_bool(attributes.get("is_async"), "is_async", kind)
            source = _render_function_source(name, params, return_annotation, body, is_async)
            node = _parse_single_statement(source, diagnostic=kind)
            if not isinstance(node, cst.FunctionDef):
                raise DeclarationError(kind, "rendered source did not produce a FunctionDef")
            if decorator_children:
                node = node.with_changes(decorators=tuple(decorator_children) + tuple(node.decorators))
            return node

        raise DeclarationError(kind, f"unsupported kind in Python backend: {kind!r}")

    # ---- insert / remove ---------------------------------------------------

    def insert_child(
        self, parent: Any, child: Any, anchor: Any | None = None, position: str = "end"
    ) -> Any:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")

        body = self._extract_body(parent)
        new_body = self._compute_new_body(body, child, anchor, position)
        return self._replace_body(parent, new_body)

    def remove_child(self, parent: Any, child: Any) -> Any:
        body = self._extract_body(parent)
        try:
            idx = body.index(child)
        except ValueError:
            raise ValueError("child is not present in parent's body") from None
        new_body = body[:idx] + body[idx + 1 :]
        return self._replace_body(parent, new_body)

    def _extract_body(self, parent: cst.CSTNode) -> list[cst.CSTNode]:
        if isinstance(parent, cst.Module):
            return list(parent.body)
        if isinstance(parent, (cst.ClassDef, cst.FunctionDef)):
            inner = parent.body
            if not isinstance(inner, cst.IndentedBlock):
                raise TypeError(f"cannot insert into {type(parent).__name__} with non-IndentedBlock body")
            return list(inner.body)
        raise TypeError(f"cannot insert child into {type(parent).__name__}")

    def _replace_body(self, parent: cst.CSTNode, new_body: Sequence[cst.CSTNode]) -> cst.CSTNode:
        if isinstance(parent, cst.Module):
            return parent.with_changes(body=tuple(new_body))
        if isinstance(parent, (cst.ClassDef, cst.FunctionDef)):
            inner = parent.body
            assert isinstance(inner, cst.IndentedBlock)
            return parent.with_changes(body=inner.with_changes(body=tuple(new_body)))
        raise TypeError(f"cannot replace body on {type(parent).__name__}")

    def _compute_new_body(
        self,
        body: list[cst.CSTNode],
        child: cst.CSTNode,
        anchor: cst.CSTNode | None,
        position: str,
    ) -> list[cst.CSTNode]:
        if position == "end":
            return body + [child]
        if position == "start":
            return [child] + body
        # before / after require anchor
        assert anchor is not None
        try:
            idx = body.index(anchor)
        except ValueError:
            raise ValueError("anchor is not present in parent's body") from None
        insert_at = idx if position == "before" else idx + 1
        return body[:insert_at] + [child] + body[insert_at:]

    # ---- pattern matching & rewriting --------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError("parse", "pattern source is empty")
        encoded, placeholders = _encode_sigils(pattern_source)
        root = _parse_pattern_fragment(encoded)
        return _PythonPattern(source=pattern_source, root=root, placeholders=placeholders)

    def find_matches(
        self, tree: Any, pattern: AstPattern, scope: Any | None = None
    ) -> Iterable[PatternMatch]:
        if not isinstance(pattern, _PythonPattern):
            raise TypeError(f"pattern must come from this backend's compile_pattern; got {type(pattern).__name__}")
        search_root = scope if scope is not None else tree
        if not isinstance(search_root, cst.CSTNode):
            raise TypeError(f"find_matches requires a CST node; got {type(search_root).__name__}")
        return list(self._iter_matches(tree, search_root, pattern))

    def _iter_matches(
        self, whole_tree: cst.CSTNode, search_root: cst.CSTNode, pattern: _PythonPattern
    ) -> Iterator[PatternMatch]:
        # visit every descendant of search_root in a single pass, recording matches
        symbol_path_map = self._symbol_path_map(whole_tree)
        collector: list[PatternMatch] = []

        class _Collector(cst.CSTVisitor):
            def on_visit(inner, node: cst.CSTNode) -> bool:
                bindings: dict[str, Any] = {}
                if _match_node(node, pattern.root, pattern.placeholders, bindings):
                    collector.append(
                        PatternMatch(
                            node=node,
                            bindings=bindings,
                            symbol_path=symbol_path_map.get(id(node)),
                        )
                    )
                return True

        search_root.visit(_Collector())
        yield from collector

    def _symbol_path_map(self, tree: cst.CSTNode) -> dict[int, str]:
        # map every CST node to its enclosing symbol path; only named-symbol nodes
        # appear directly in _walk_named_symbols, so we extend by descending and
        # tagging everything beneath each symbol with that symbol's path.
        mapping: dict[int, str] = {}
        if not isinstance(tree, cst.Module):
            return mapping
        for path, _kind, node in _walk_named_symbols(tree, prefix="", inside_class=False):
            self._tag_subtree(node, path, mapping)
        return mapping

    def _tag_subtree(self, node: cst.CSTNode, path: str, mapping: dict[int, str]) -> None:
        class _Tagger(cst.CSTVisitor):
            def on_visit(inner, descendant: cst.CSTNode) -> bool:
                mapping.setdefault(id(descendant), path)
                return True

        node.visit(_Tagger())

    def render_replacement(
        self, replacement_source: str, bindings: Mapping[str, Any]
    ) -> cst.CSTNode:
        encoded, placeholders = _encode_sigils(replacement_source)
        # ensure every placeholder actually has a binding (or is a wildcard)
        for placeholder in placeholders.values():
            if placeholder.name == "_":
                raise PatternError(
                    "parse",
                    f"replacement source contains a wildcard {placeholder.encoded!r} which cannot be filled",
                )
            if placeholder.name not in bindings:
                raise PatternError(
                    "parse",
                    f"replacement references capture {placeholder.name!r} with no binding",
                )
        root = _parse_pattern_fragment(encoded)
        # substitute captured nodes in place of placeholder identifiers
        transformer = _BindingSubstitutor(placeholders=placeholders, bindings=bindings)
        rendered = root.visit(transformer)
        if not isinstance(rendered, cst.CSTNode):
            raise PatternError("parse", "binding substitution removed the replacement root")
        return rendered

    def apply_replacement(
        self, tree: Any, match: PatternMatch, replacement: Any
    ) -> cst.CSTNode:
        if not isinstance(tree, cst.CSTNode):
            raise TypeError(f"tree must be a CST node; got {type(tree).__name__}")
        if not isinstance(match.node, cst.CSTNode) or not isinstance(replacement, cst.CSTNode):
            raise TypeError("match.node and replacement must be CST nodes")
        transformer = _NodeReplacer(target=match.node, replacement=replacement)
        new_tree = tree.visit(transformer)
        if not transformer.replaced:
            raise PatternError("no-match", "match node was not found in tree; ensure match came from this tree")
        if not isinstance(new_tree, cst.CSTNode):
            raise PatternError("no-match", "replacement removed the tree root")
        return new_tree

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> cst.Module:
        if source_kind != "module":
            raise DeclarationError(source_kind, f"python has no source kind {source_kind!r}")
        # parse the empty string so the resulting Module has libcst's canonical
        # metadata for an empty source (serializes to "" rather than "\n")
        return cst.parse_module("")


# =============================================================================
# CST transformers supporting pattern replacement
# =============================================================================


class _BindingSubstitutor(cst.CSTTransformer):
    """Replace encoded placeholder ``Name`` nodes with their captured values."""

    def __init__(self, placeholders: Mapping[str, _Placeholder], bindings: Mapping[str, Any]):
        super().__init__()
        self._placeholders = placeholders
        self._bindings = bindings

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> Any:  # type: ignore[override]
        info = self._placeholders.get(updated_node.value)
        if info is None:
            return updated_node
        bound = self._bindings[info.name]
        return bound


class _NodeReplacer(cst.CSTTransformer):
    """Replace a specific ``target`` node (by identity) with ``replacement``."""

    def __init__(self, target: cst.CSTNode, replacement: cst.CSTNode):
        super().__init__()
        self._target = target
        self._replacement = replacement
        self.replaced = False

    def on_leave(self, original_node: cst.CSTNode, updated_node: cst.CSTNode) -> Any:  # type: ignore[override]
        if original_node is self._target:
            self.replaced = True
            return self._replacement
        return updated_node
