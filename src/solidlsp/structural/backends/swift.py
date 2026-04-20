"""Swift structural backend.

Bridges :class:`~solidlsp.structural.base.StructuralLanguage` to SwiftSyntax,
which lives on the Swift side as a small CLI (see :mod:`swift_bridge`).

The handle shape mirrors the C++ backend: parse wraps source verbatim, every
mutation applies an edit and re-parses via the subprocess, and symbols are
value-typed refs (kind + name_path + byte offsets) that survive across
re-parses.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

# -----------------------------------------------------------------------------
# Attribute specs
# -----------------------------------------------------------------------------

_ATTR_NAME = AttributeSpec(
    name="name",
    type_hint="str",
    required=True,
    description="The symbol's spelling as it appears in the declaration.",
)

_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=False,
    description="Raw source text placed inside the declaration's braces.",
)

_ATTR_STATEMENT = AttributeSpec(
    name="statement",
    type_hint="str",
    required=True,
    description="The full Swift statement text, rendered verbatim.",
)

_ATTR_INHERITANCE = AttributeSpec(
    name="inheritance",
    type_hint="list[str]",
    required=False,
    description="Base class / protocol list appended after ``:``.",
)

_ATTR_GENERIC_PARAMETERS = AttributeSpec(
    name="generic_parameters",
    type_hint="str",
    required=False,
    description="Generic parameter clause without angle brackets (e.g. ``T, U: P``).",
)

_ATTR_PARAMETERS = AttributeSpec(
    name="parameters",
    type_hint="str",
    required=False,
    description="Parameter list interior, no parentheses.",
)

_ATTR_RETURN = AttributeSpec(
    name="return_type",
    type_hint="str",
    required=False,
    description="Return type expression (without ``->``).",
)

_ATTR_MODIFIERS = AttributeSpec(
    name="modifiers",
    type_hint="str",
    required=False,
    description="Modifier tokens placed before the decl keyword (``public``, ``static override``, etc.).",
)

_ATTR_EXTENDED_TYPE = AttributeSpec(
    name="extended_type",
    type_hint="str",
    required=True,
    description="Type the extension extends.",
)

_ATTR_TYPE = AttributeSpec(
    name="type",
    type_hint="str",
    required=False,
    description="Explicit type annotation (after ``:``).",
)

_ATTR_INITIALIZER = AttributeSpec(
    name="initializer",
    type_hint="str",
    required=False,
    description="Initializer expression (after ``=``).",
)

_ATTR_IS_LET = AttributeSpec(
    name="is_let",
    type_hint="bool",
    required=False,
    description="If true, declare via ``let``; otherwise ``var``.",
)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


def swift_kind_schema() -> KindSchema:
    """Return the :class:`KindSchema` exposed by the Swift backend."""
    # top-level placement at source root
    source_file = KindSpec(
        name="source_file",
        description="A Swift source file; the root of any parsed tree.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(
            {
                "import",
                "class",
                "struct",
                "enum",
                "protocol",
                "extension",
                "actor",
                "function",
                "variable",
                "type_alias",
            }
        ),
    )
    imp = KindSpec(
        name="import",
        description="A Swift ``import`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )

    # type-like containers
    type_children = frozenset(
        {
            "function",
            "method",
            "initializer",
            "property",
            "variable",
            "type_alias",
            "class",
            "struct",
            "enum",
            "protocol",
            "extension",
            "actor",
            "enum_case",
        }
    )
    type_parents = frozenset(
        {
            "source_file",
            "class",
            "struct",
            "enum",
            "protocol",
            "extension",
            "actor",
        }
    )

    cls = KindSpec(
        name="class",
        description="A Swift ``class`` declaration.",
        attributes=(_ATTR_NAME, _ATTR_BODY, _ATTR_INHERITANCE, _ATTR_MODIFIERS, _ATTR_GENERIC_PARAMETERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=type_children,
    )
    struct = KindSpec(
        name="struct",
        description="A Swift ``struct`` declaration.",
        attributes=(_ATTR_NAME, _ATTR_BODY, _ATTR_INHERITANCE, _ATTR_MODIFIERS, _ATTR_GENERIC_PARAMETERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=type_children,
    )
    enum = KindSpec(
        name="enum",
        description="A Swift ``enum`` declaration.",
        attributes=(_ATTR_NAME, _ATTR_BODY, _ATTR_INHERITANCE, _ATTR_MODIFIERS, _ATTR_GENERIC_PARAMETERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=type_children,
    )
    protocol = KindSpec(
        name="protocol",
        description="A Swift ``protocol`` declaration.",
        attributes=(_ATTR_NAME, _ATTR_BODY, _ATTR_INHERITANCE, _ATTR_MODIFIERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=type_children,
    )
    extension = KindSpec(
        name="extension",
        description="A Swift ``extension`` declaration.",
        attributes=(_ATTR_EXTENDED_TYPE, _ATTR_BODY, _ATTR_INHERITANCE, _ATTR_MODIFIERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=type_children,
    )
    actor = KindSpec(
        name="actor",
        description="A Swift ``actor`` declaration.",
        attributes=(_ATTR_NAME, _ATTR_BODY, _ATTR_INHERITANCE, _ATTR_MODIFIERS, _ATTR_GENERIC_PARAMETERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=type_children,
    )

    # callables
    function = KindSpec(
        name="function",
        description="A top-level Swift function.",
        attributes=(_ATTR_NAME, _ATTR_PARAMETERS, _ATTR_RETURN, _ATTR_BODY, _ATTR_MODIFIERS, _ATTR_GENERIC_PARAMETERS),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    method = KindSpec(
        name="method",
        description="A method declared inside a type body.",
        attributes=(_ATTR_NAME, _ATTR_PARAMETERS, _ATTR_RETURN, _ATTR_BODY, _ATTR_MODIFIERS, _ATTR_GENERIC_PARAMETERS),
        allowed_parent_kinds=frozenset({"class", "struct", "enum", "protocol", "extension", "actor"}),
        allowed_child_kinds=frozenset(),
    )
    initializer = KindSpec(
        name="initializer",
        description="An ``init`` declaration within a type body.",
        attributes=(_ATTR_PARAMETERS, _ATTR_BODY, _ATTR_MODIFIERS, _ATTR_GENERIC_PARAMETERS),
        allowed_parent_kinds=frozenset({"class", "struct", "enum", "extension", "actor"}),
        allowed_child_kinds=frozenset(),
    )

    # storage
    variable = KindSpec(
        name="variable",
        description="A top-level ``var`` or ``let`` binding.",
        attributes=(_ATTR_NAME, _ATTR_TYPE, _ATTR_INITIALIZER, _ATTR_IS_LET, _ATTR_MODIFIERS),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    prop = KindSpec(
        name="property",
        description="A stored or computed property inside a type body.",
        attributes=(_ATTR_NAME, _ATTR_TYPE, _ATTR_INITIALIZER, _ATTR_IS_LET, _ATTR_MODIFIERS, _ATTR_BODY),
        allowed_parent_kinds=frozenset({"class", "struct", "enum", "protocol", "extension", "actor"}),
        allowed_child_kinds=frozenset(),
    )

    # leaves
    alias = KindSpec(
        name="type_alias",
        description="A ``typealias`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file", "class", "struct", "enum", "protocol", "extension", "actor"}),
        allowed_child_kinds=frozenset(),
    )
    enum_case = KindSpec(
        name="enum_case",
        description="An ``case`` declaration inside an ``enum`` body.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"enum"}),
        allowed_child_kinds=frozenset(),
    )

    specs = (
        source_file,
        imp,
        cls,
        struct,
        enum,
        protocol,
        extension,
        actor,
        function,
        method,
        initializer,
        variable,
        prop,
        alias,
        enum_case,
    )
    return KindSchema(
        language_key="swift",
        source_kinds=frozenset({"source_file"}),
        kinds={s.name: s for s in specs},
    )


_SWIFT_KIND_SCHEMA = swift_kind_schema()


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


_SWIFT_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


class SwiftLogicalNameResolver:
    """Minimal Swift logical-name resolver.

    Swift's module model differs from Python's package model: a module is a
    build target, not a filesystem path. For M3 we treat a logical name as a
    single identifier mapped to ``<project_root>/<name>.swift`` — sufficient
    for create_source / rename flows Serena needs from the resolver.
    """

    def __init__(self, project_root: Path):
        """:param project_root: project root under which Swift sources live."""
        self._project_root = project_root

    def parse(self, raw: str) -> LogicalName:
        # single identifier only — dotted Swift module paths are not modelled
        if not _SWIFT_IDENTIFIER_RE.match(raw):
            raise NameResolutionError(raw, "swift logical names must be a single identifier")
        return LogicalName(raw=raw, parts=(raw,))

    def resolve(self, name: LogicalName) -> NameResolution:
        if len(name.parts) != 1:
            raise NameResolutionError(name.raw, "swift logical names must have exactly one segment")
        candidate = self._project_root / f"{name.parts[0]}.swift"
        exists = candidate.is_file()
        try:
            relative = candidate.relative_to(self._project_root)
        except ValueError as err:
            raise NameResolutionError(
                str(candidate),
                f"resolved path {candidate} escapes project root {self._project_root}",
            ) from err
        return NameResolution(relative_path=str(relative), source_kind="source_file", exists=exists)


# -----------------------------------------------------------------------------
# Handles
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _SwiftTree:
    """Opaque handle for a parsed Swift source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    """

    source: str


@dataclass(frozen=True)
class _SwiftSymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    Mirrors :class:`_CppSymbolRef`. Byte offsets let the symbol survive
    re-parse after a mutation that invalidates any opaque AST handles.

    :ivar kind: structural kind name.
    :ivar name_path: ``Parent/Child`` path within the source file.
    :ivar extent_offset: start byte of the symbol's declaration.
    :ivar extent_length: byte length of the symbol's declaration.
    :ivar body_range: for compound decls, ``(inner_start, inner_end)``
        between the ``{`` and ``}`` braces exclusive. ``None`` for leaves.
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _SwiftDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text, ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _SwiftPattern:
    """A compiled Swift pattern.

    :ivar source: pattern source text with ``$name`` / ``$_`` sigils.
    """

    source: str


# -----------------------------------------------------------------------------
# Subprocess bridge
# -----------------------------------------------------------------------------

_BRIDGE_PACKAGE_DIR = Path(__file__).resolve().parent / "swift_bridge"
_BRIDGE_BINARY_RELATIVE = Path(".build") / "release" / "serena-swift-bridge"


def _bridge_binary_path() -> Path:
    """Absolute path to the built bridge executable."""
    return _BRIDGE_PACKAGE_DIR / _BRIDGE_BINARY_RELATIVE


def _build_bridge_if_needed() -> Path:
    """Build the Swift bridge if its binary is absent; return the path."""
    binary = _bridge_binary_path()
    if binary.exists():
        return binary
    # serialize concurrent first-use builds under a file lock
    lock_path = _BRIDGE_PACKAGE_DIR / ".build.lock"
    _BRIDGE_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        try:
            # posix advisory lock; no-op on systems without fcntl
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        if binary.exists():
            return binary
        result = subprocess.run(
            ["swift", "build", "-c", "release"],
            check=False,
            cwd=str(_BRIDGE_PACKAGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ParseError(
                language_key="swift",
                source_preview="",
                detail=f"failed to build serena-swift-bridge:\n{result.stderr}",
            )
    if not binary.exists():
        raise ParseError(
            language_key="swift",
            source_preview="",
            detail=f"bridge build reported success but binary is missing at {binary}",
        )
    return binary


class _SwiftBridge:
    """Long-lived subprocess wrapping ``serena-swift-bridge``.

    One bridge is shared by a single :class:`SwiftStructuralLanguage`
    instance. Calls are serialized by a mutex so concurrent Python callers
    do not interleave their JSON frames.
    """

    def __init__(self, binary_path: Path):
        """:param binary_path: absolute path to the serena-swift-bridge
        executable.
        """
        self._binary_path = binary_path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        # lazy spawn; detect crashed / terminated children and respawn
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                [str(self._binary_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        return self._process

    def call(self, op: str, **payload: Any) -> dict[str, Any]:
        """Send one request and return the decoded response dict."""
        with self._lock:
            proc = self._ensure_started()
            assert proc.stdin is not None and proc.stdout is not None
            request = {"op": op, **payload}
            body = json.dumps(request).encode("utf-8")
            prefix = struct.pack("<I", len(body))
            try:
                proc.stdin.write(prefix + body)
                proc.stdin.flush()
                length_bytes = _read_exact(proc.stdout, 4)
                (length,) = struct.unpack("<I", length_bytes)
                response_bytes = _read_exact(proc.stdout, length)
            except (BrokenPipeError, OSError) as exc:
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                raise ParseError(
                    language_key="swift",
                    source_preview="",
                    detail=f"bridge communication failed: {exc}; stderr:\n{stderr}",
                )
            return json.loads(response_bytes)

    def close(self) -> None:
        """Shut the bridge down cleanly; idempotent."""
        with self._lock:
            if self._process is None:
                return
            try:
                if self._process.stdin is not None:
                    self._process.stdin.close()
            except OSError:
                pass
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _read_exact(stream: Any, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise ``OSError`` at EOF."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise OSError("bridge closed stdout unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# -----------------------------------------------------------------------------
# Declaration renderers
# -----------------------------------------------------------------------------


def _require_str(attrs: Mapping[str, Any], name: str, kind: KindName) -> str:
    if name not in attrs:
        raise DeclarationError(kind, f"missing required attribute {name!r}")
    value = attrs[name]
    if not isinstance(value, str):
        raise DeclarationError(kind, f"{name!r} must be a str; got {type(value).__name__}")
    return value


def _optional_str(attrs: Mapping[str, Any], name: str, kind: KindName) -> str:
    value = attrs.get(name, "")
    if not isinstance(value, str):
        raise DeclarationError(kind, f"{name!r} must be a str; got {type(value).__name__}")
    return value


def _optional_str_or_none(attrs: Mapping[str, Any], name: str, kind: KindName) -> str | None:
    if name not in attrs or attrs[name] is None:
        return None
    value = attrs[name]
    if not isinstance(value, str):
        raise DeclarationError(kind, f"{name!r} must be a str or None; got {type(value).__name__}")
    return value


def _optional_str_list(attrs: Mapping[str, Any], name: str, kind: KindName) -> list[str]:
    if name not in attrs or attrs[name] is None:
        return []
    value = attrs[name]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise DeclarationError(kind, f"{name!r} must be a list[str]; got {type(value).__name__}")
    return list(value)


def _optional_bool(attrs: Mapping[str, Any], name: str, kind: KindName) -> bool:
    if name not in attrs or attrs[name] is None:
        return False
    value = attrs[name]
    if not isinstance(value, bool):
        raise DeclarationError(kind, f"{name!r} must be a bool; got {type(value).__name__}")
    return value


def _ensure_trailing_newline(src: str) -> str:
    return src if src.endswith("\n") else src + "\n"


def _concat_children_source(children: Sequence[_SwiftDeclaration]) -> str:
    # join each child's source; callers insert inside a brace block
    return "".join(c.source for c in children)


def _join_bodies(body: str, children_source: str) -> str:
    # body is the raw block body attribute; children_source is rendered children
    parts = [body, children_source]
    joined = "".join(p for p in parts if p)
    if joined and not joined.endswith("\n"):
        joined += "\n"
    return joined


def _inheritance_clause(inheritance: list[str]) -> str:
    return f": {', '.join(inheritance)}" if inheritance else ""


def _generic_clause(generic: str | None) -> str:
    return f"<{generic}>" if generic else ""


def _modifier_prefix(modifiers: str) -> str:
    return f"{modifiers} " if modifiers else ""


# -----------------------------------------------------------------------------
# Pattern rendering (replacement templating)
# -----------------------------------------------------------------------------


_CAPTURE_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)")


def _render_replacement_template(replacement_source: str, bindings: Mapping[str, Any]) -> str:
    """Substitute ``$name`` references in ``replacement_source`` with
    ``bindings[name]`` converted to source text.

    Bindings may be raw strings (pre-rendered snippets) or
    :class:`_SwiftDeclaration` handles.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise PatternError(reason="binding", detail=f"no binding for ${name}")
        value = bindings[name]
        if isinstance(value, _SwiftDeclaration):
            return value.source
        if isinstance(value, str):
            return value
        raise PatternError(
            reason="binding",
            detail=f"binding ${name} has unsupported type {type(value).__name__}",
        )

    return _CAPTURE_REF_RE.sub(_sub, replacement_source)


# -----------------------------------------------------------------------------
# Backend
# -----------------------------------------------------------------------------


class SwiftStructuralLanguage(StructuralLanguage):
    """Swift structural-language backend driven by SwiftSyntax via subprocess.

    One backend instance owns one long-lived bridge subprocess. Disposing
    the instance (``close()`` or GC) tears the subprocess down cleanly.
    """

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        binary_path: Path | None = None,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`SwiftLogicalNameResolver` rooted at CWD.
        :param binary_path: explicit path to the bridge binary. If ``None``,
            builds from the package source at first use.
        """
        self._name_resolver = name_resolver or SwiftLogicalNameResolver(Path.cwd())
        self._explicit_binary = binary_path
        self._bridge: _SwiftBridge | None = None

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "swift"

    @property
    def kind_schema(self) -> KindSchema:
        return _SWIFT_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- bridge access -----------------------------------------------------

    def _get_bridge(self) -> _SwiftBridge:
        if self._bridge is None:
            binary = self._explicit_binary or _build_bridge_if_needed()
            self._bridge = _SwiftBridge(binary)
        return self._bridge

    def close(self) -> None:
        """Tear down the bridge subprocess; subsequent ops respawn it."""
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ---- parse / serialize -------------------------------------------------

    def parse(self, source: str) -> _SwiftTree:
        # SwiftSyntax is byte-identical by construction; we wrap verbatim
        # without an up-front subprocess round trip
        return _SwiftTree(source=source)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _SwiftTree):
            return tree.source
        if isinstance(tree, _SwiftDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _SwiftTree):
            raise TypeError(f"root_kind expects a _SwiftTree; got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _SwiftTree):
            raise TypeError(f"walk_symbols expects a _SwiftTree; got {type(tree).__name__}")
        response = self._get_bridge().call("walk_symbols", source=tree.source)
        _raise_for_error(response)
        for entry in response.get("symbols", []):
            body = entry.get("body_range")
            ref = _SwiftSymbolRef(
                kind=entry["kind"],
                name_path=entry["name_path"],
                extent_offset=int(entry["extent_offset"]),
                extent_length=int(entry["extent_length"]),
                body_range=(int(body[0]), int(body[1])) if body else None,
            )
            yield (ref.name_path, ref.kind, ref)

    # ---- declaration -------------------------------------------------------

    def build_declaration(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
        children: Iterable[Any],
    ) -> _SwiftDeclaration:
        self.kind_schema.get(kind)  # validate kind exists
        children_list = list(children)

        for child in children_list:
            if not isinstance(child, _SwiftDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _SwiftDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind == "import":
            statement = _require_str(attributes, "statement", kind)
            stripped = statement.lstrip()
            if not stripped.startswith("import "):
                raise DeclarationError(kind, "import statement must start with 'import '")
            return _SwiftDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind in {"class", "struct", "enum", "actor"}:
            name = _require_str(attributes, "name", kind)
            body = _optional_str(attributes, "body", kind)
            inheritance = _optional_str_list(attributes, "inheritance", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            generic = _optional_str_or_none(attributes, "generic_parameters", kind)
            inner = _join_bodies(body, _concat_children_source(children_list))
            rendered = (
                f"{_modifier_prefix(modifiers)}{kind} {name}{_generic_clause(generic)}{_inheritance_clause(inheritance)} {{\n{inner}}}\n"
            )
            return _SwiftDeclaration(kind=kind, source=rendered)

        if kind == "protocol":
            name = _require_str(attributes, "name", kind)
            body = _optional_str(attributes, "body", kind)
            inheritance = _optional_str_list(attributes, "inheritance", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            inner = _join_bodies(body, _concat_children_source(children_list))
            rendered = f"{_modifier_prefix(modifiers)}protocol {name}{_inheritance_clause(inheritance)} {{\n{inner}}}\n"
            return _SwiftDeclaration(kind=kind, source=rendered)

        if kind == "extension":
            extended = _require_str(attributes, "extended_type", kind)
            body = _optional_str(attributes, "body", kind)
            inheritance = _optional_str_list(attributes, "inheritance", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            inner = _join_bodies(body, _concat_children_source(children_list))
            rendered = f"{_modifier_prefix(modifiers)}extension {extended}{_inheritance_clause(inheritance)} {{\n{inner}}}\n"
            return _SwiftDeclaration(kind=kind, source=rendered)

        if kind in {"function", "method"}:
            name = _require_str(attributes, "name", kind)
            params = _optional_str(attributes, "parameters", kind)
            ret = _optional_str_or_none(attributes, "return_type", kind)
            body = _optional_str(attributes, "body", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            generic = _optional_str_or_none(attributes, "generic_parameters", kind)
            ret_clause = f" -> {ret}" if ret else ""
            rendered = (
                f"{_modifier_prefix(modifiers)}func {name}{_generic_clause(generic)}"
                f"({params}){ret_clause} {{\n{body}{'' if body.endswith(chr(10)) or not body else chr(10)}}}\n"
            )
            return _SwiftDeclaration(kind=kind, source=rendered)

        if kind == "initializer":
            params = _optional_str(attributes, "parameters", kind)
            body = _optional_str(attributes, "body", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            generic = _optional_str_or_none(attributes, "generic_parameters", kind)
            rendered = (
                f"{_modifier_prefix(modifiers)}init{_generic_clause(generic)}"
                f"({params}) {{\n{body}{'' if body.endswith(chr(10)) or not body else chr(10)}}}\n"
            )
            return _SwiftDeclaration(kind=kind, source=rendered)

        if kind in {"variable", "property"}:
            name = _require_str(attributes, "name", kind)
            ty = _optional_str_or_none(attributes, "type", kind)
            init = _optional_str_or_none(attributes, "initializer", kind)
            is_let = _optional_bool(attributes, "is_let", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            body_opt: str | None = _optional_str_or_none(attributes, "body", kind) if kind == "property" else None
            keyword = "let" if is_let else "var"
            type_clause = f": {ty}" if ty else ""
            init_clause = f" = {init}" if init is not None else ""
            body_clause = f" {{\n{body_opt}{'' if body_opt.endswith(chr(10)) else chr(10)}}}" if body_opt else ""
            rendered = f"{_modifier_prefix(modifiers)}{keyword} {name}{type_clause}{init_clause}{body_clause}\n"
            return _SwiftDeclaration(kind=kind, source=rendered)

        if kind == "type_alias":
            statement = _require_str(attributes, "statement", kind)
            if not statement.lstrip().startswith("typealias "):
                raise DeclarationError(kind, "type_alias statement must start with 'typealias '")
            return _SwiftDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "enum_case":
            statement = _require_str(attributes, "statement", kind)
            if not statement.lstrip().startswith("case "):
                raise DeclarationError(kind, "enum_case statement must start with 'case '")
            return _SwiftDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        raise DeclarationError(kind, f"unsupported kind in Swift backend: {kind!r}")

    # ---- mutation ----------------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _SwiftTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _SwiftDeclaration):
            raise TypeError(f"child must be a _SwiftDeclaration; got {type(child).__name__}")

        tree, parent_path = self._resolve_insertion_parent(parent)
        anchor_path = anchor.name_path if isinstance(anchor, _SwiftSymbolRef) else None
        response = self._get_bridge().call(
            "insert_child",
            source=tree.source,
            parent_name_path=parent_path,
            anchor_name_path=anchor_path,
            position=position,
            child_source=child.source,
        )
        _raise_for_error(response)
        return _SwiftTree(source=response["source"])

    def remove_child(self, parent: Any, child: Any) -> _SwiftTree:
        if not isinstance(child, _SwiftSymbolRef):
            raise TypeError(f"child must be a _SwiftSymbolRef; got {type(child).__name__}")
        tree, _ = self._resolve_insertion_parent(parent)
        response = self._get_bridge().call(
            "remove_child",
            source=tree.source,
            child_name_path=child.name_path,
        )
        _raise_for_error(response)
        return _SwiftTree(source=response["source"])

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_SwiftTree, str | None]:
        """Normalize ``parent`` to ``(tree, parent_name_path_or_None)``.

        Accepts either a whole tree (parent becomes None → root source file)
        or a symbol ref (whose ``name_path`` becomes the parent). The tree
        must be supplied separately via state, so we require the caller to
        pass either a tree or (tree, ref). For ergonomic parity with the
        C++ backend, a bare ``_SwiftSymbolRef`` is not accepted — agents
        should pass the tree and use ``anchor`` for ref-based placement.
        """
        if isinstance(parent, _SwiftTree):
            return parent, None
        raise TypeError(
            f"parent must be a _SwiftTree; got {type(parent).__name__}. Pass the tree and use anchor / position to target a nested symbol."
        )

    # ---- pattern matching --------------------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError(reason="parse", detail="empty pattern")
        return _SwiftPattern(source=pattern_source)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _SwiftTree):
            raise TypeError(f"find_matches expects a _SwiftTree; got {type(tree).__name__}")
        if not isinstance(pattern, _SwiftPattern):
            raise TypeError(f"pattern must be a _SwiftPattern; got {type(pattern).__name__}")
        scope_path: str | None = None
        if isinstance(scope, _SwiftSymbolRef):
            scope_path = scope.name_path
        response = self._get_bridge().call(
            "find_matches",
            source=tree.source,
            pattern_source=pattern.source,
            scope_name_path=scope_path,
        )
        _raise_for_error(response)
        for match in response.get("matches", []):
            bindings = {name: info["source"] for name, info in match.get("bindings", {}).items()}
            node_ref = _SwiftSymbolRef(
                kind="match",
                name_path="",
                extent_offset=int(match["extent_offset"]),
                extent_length=int(match["extent_length"]),
                body_range=None,
            )
            yield PatternMatch(node=node_ref, bindings=bindings, symbol_path=None)

    def render_replacement(
        self,
        replacement_source: str,
        bindings: Mapping[str, Any],
    ) -> _SwiftDeclaration:
        if not replacement_source:
            raise PatternError(reason="parse", detail="empty replacement")
        rendered = _render_replacement_template(replacement_source, bindings)
        return _SwiftDeclaration(kind="_replacement", source=rendered)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _SwiftTree:
        if not isinstance(tree, _SwiftTree):
            raise TypeError(f"apply_replacement expects a _SwiftTree; got {type(tree).__name__}")
        if not isinstance(replacement, _SwiftDeclaration):
            raise TypeError(f"replacement must be a _SwiftDeclaration; got {type(replacement).__name__}")
        if not isinstance(match.node, _SwiftSymbolRef):
            raise TypeError(f"match.node must be a _SwiftSymbolRef; got {type(match.node).__name__}")
        response = self._get_bridge().call(
            "apply_replacement",
            source=tree.source,
            match_offset=match.node.extent_offset,
            match_length=match.node.extent_length,
            replacement_source=replacement.source,
        )
        _raise_for_error(response)
        return _SwiftTree(source=response["source"])

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _SwiftTree:
        if source_kind not in self.kind_schema.source_kinds:
            raise DeclarationError(
                source_kind,
                f"not a source kind in {self.language_key}; valid: {sorted(self.kind_schema.source_kinds)}",
            )
        return _SwiftTree(source="")


def _raise_for_error(response: Mapping[str, Any]) -> None:
    """Map a bridge error response onto the structural error hierarchy."""
    if response.get("ok", False):
        return
    kind = str(response.get("error_kind", "bridge"))
    message = str(response.get("message", ""))
    if kind == "pattern_parse":
        raise PatternError(reason="parse", detail=message)
    if kind in {"symbol_missing", "no_body", "bad_request"}:
        raise ValueError(f"swift bridge: {message}")
    if kind == "unknown_op":
        raise RuntimeError(f"swift bridge: {message}")
    raise ParseError(language_key="swift", source_preview="", detail=f"{kind}: {message}")
