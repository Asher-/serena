"""C# structural backend.

Bridges :class:`~solidlsp.structural.base.StructuralLanguage` to
Microsoft.CodeAnalysis.CSharp (Roslyn), which runs as a long-lived .NET
subprocess (see :mod:`csharp_bridge`).

The handle shape mirrors the Swift / TypeScript / Go / Rust / Java / Ruby
backends: parse wraps source verbatim, every mutation applies a text-level
edit and re-parses via the subprocess, and symbols are value-typed refs
(kind + name_path + byte offsets) that survive across re-parses. Byte
offsets on the wire are UTF-8 byte offsets, computed in the bridge from
Roslyn's UTF-16 character positions.

The bridge is built lazily on first use via ``dotnet build --configuration
Release`` under a file lock; the resulting ``SerenaCSharpBridge.dll`` is
invoked via ``dotnet exec``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import threading
from collections.abc import Iterable, Mapping
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

_ATTR_STATEMENT = AttributeSpec(
    name="statement",
    type_hint="str",
    required=True,
    description="The full C# statement text, rendered verbatim.",
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
    description="Return-type expression (e.g. ``void``, ``int``, ``List<string>``).",
)

_ATTR_GENERICS = AttributeSpec(
    name="generics",
    type_hint="str",
    required=False,
    description="Generic parameter clause without angle brackets (e.g. ``T, U`` or ``T`` with constraints rendered inline).",
)

_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=False,
    description="Method / constructor body text placed inside the braces.",
)

_ATTR_MODIFIERS = AttributeSpec(
    name="modifiers",
    type_hint="str",
    required=False,
    description="Modifier tokens placed before the item (e.g. ``public``, ``public static``, ``private readonly``).",
)

_ATTR_ATTRIBUTES = AttributeSpec(
    name="attributes",
    type_hint="str",
    required=False,
    description='Attribute lists placed before modifiers (e.g. ``[Serializable]`` or ``[Obsolete("use Y")]``).',
)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


def csharp_kind_schema() -> KindSchema:
    """Return the :class:`KindSchema` exposed by the C# backend.

    The vocabulary captures top-level constructs (using, namespace, class,
    struct, interface, enum, record, delegate) plus the five major member
    kinds (method, property, field, event, constructor) for in-body
    navigation. Members have empty ``allowed_parent_kinds`` because their
    containing types do not expose body ranges in v1 -- to add a member
    to an existing type, agents rebuild the type block. Mirrors Java's
    "top-level + one layer of walk" stance.
    """
    source_file = KindSpec(
        name="source_file",
        description="A C# compilation unit; the root of any parsed tree.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(
            {
                "using",
                "namespace",
                "class",
                "struct",
                "interface",
                "enum",
                "record",
                "delegate",
            }
        ),
    )
    using_item = KindSpec(
        name="using",
        description="A C# ``using`` directive (plain, static, alias, or global).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    namespace_item = KindSpec(
        name="namespace",
        description="A C# ``namespace`` declaration (block form or file-scoped).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    class_item = KindSpec(
        name="class",
        description="A C# ``class`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    struct_item = KindSpec(
        name="struct",
        description="A C# ``struct`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    interface_item = KindSpec(
        name="interface",
        description="A C# ``interface`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    enum_item = KindSpec(
        name="enum",
        description="A C# ``enum`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    record_item = KindSpec(
        name="record",
        description="A C# ``record`` declaration (class or struct flavor).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    delegate_item = KindSpec(
        name="delegate",
        description="A C# ``delegate`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    method_item = KindSpec(
        name="method",
        description=(
            "A C# method inside a class / struct / interface / record. Walked and "
            "removable in v1; inserting a method into an existing type requires "
            "rebuilding the type because type body ranges are not exposed."
        ),
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_GENERICS,
            _ATTR_MODIFIERS,
            _ATTR_ATTRIBUTES,
        ),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    property_item = KindSpec(
        name="property",
        description=(
            "A C# property inside a class / struct / interface / record. "
            "Walked and removable in v1; inserting a property into an "
            "existing type requires rebuilding the type."
        ),
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    field_item = KindSpec(
        name="field",
        description=(
            "A C# field declaration inside a class / struct / record. "
            "Walked and removable in v1; inserting a field into an "
            "existing type requires rebuilding the type. Multi-variable "
            "declarations (``int a, b;``) yield one walk entry per variable."
        ),
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    event_item = KindSpec(
        name="event",
        description=(
            "A C# event declaration (``event EventHandler E;`` or "
            "``event EventHandler E { add; remove; }``). Walked and "
            "removable in v1; not insertable into an existing type."
        ),
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    constructor_item = KindSpec(
        name="constructor",
        description=(
            "A C# constructor inside a class / struct / record. Walked and "
            "removable in v1; inserting a constructor into an existing "
            "type requires rebuilding the type."
        ),
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_BODY,
            _ATTR_MODIFIERS,
            _ATTR_ATTRIBUTES,
        ),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )

    specs = (
        source_file,
        using_item,
        namespace_item,
        class_item,
        struct_item,
        interface_item,
        enum_item,
        record_item,
        delegate_item,
        method_item,
        property_item,
        field_item,
        event_item,
        constructor_item,
    )
    return KindSchema(
        language_key="csharp",
        source_kinds=frozenset({"source_file"}),
        kinds={s.name: s for s in specs},
    )


_CSHARP_KIND_SCHEMA = csharp_kind_schema()


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


_CSHARP_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


class CSharpLogicalNameResolver:
    """C# logical-name resolver.

    Logical names are dotted namespace paths (``Foo.Bar.Baz``) that map to
    ``<project_root>/Foo/Bar/Baz.cs``. Each segment must be a valid C#
    identifier. File-level resolver -- intra-file navigation is
    :meth:`CSharpStructuralLanguage.walk_symbols`' job, not the resolver's.
    """

    def __init__(self, project_root: Path):
        self._project_root = project_root

    def parse(self, raw: str) -> LogicalName:
        if not raw:
            raise NameResolutionError(raw, "csharp logical name must be non-empty")
        parts = tuple(raw.split("."))
        for segment in parts:
            if not _CSHARP_IDENTIFIER_RE.match(segment):
                raise NameResolutionError(
                    raw,
                    f"invalid identifier segment: {segment!r}",
                )
        return LogicalName(raw=raw, parts=parts)

    def resolve(self, name: LogicalName) -> NameResolution:
        if not name.parts:
            raise NameResolutionError(name.raw, "csharp logical name must have at least one segment")
        *dirs, last = name.parts
        candidate = self._project_root.joinpath(*dirs, f"{last}.cs")
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
class _CSharpTree:
    """Opaque handle for a parsed C# source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    """

    source: str


@dataclass(frozen=True)
class _CSharpSymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    :ivar kind: structural kind name.
    :ivar name_path: ``Parent/Child`` path within the source file, with
        ``.`` between namespace segments and ``/`` between lexical levels
        (namespace / type / member).
    :ivar extent_offset: start byte of the symbol's declaration.
    :ivar extent_length: byte length of the symbol's declaration.
    :ivar body_range: always ``None`` in v1 -- C# body ranges are not
        exposed (no nested-symbol insertion into existing types).
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _CSharpDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text, ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _CSharpPattern:
    """A compiled C# pattern.

    :ivar source: pattern source text with ``$name`` / ``$_`` sigils.
    """

    source: str


# -----------------------------------------------------------------------------
# Subprocess bridge
# -----------------------------------------------------------------------------

_BRIDGE_PACKAGE_DIR = Path(__file__).resolve().parent / "csharp_bridge"
_BRIDGE_DLL_RELATIVE = Path("bin") / "Release" / "net10.0" / "SerenaCSharpBridge.dll"


def _bridge_dll_path() -> Path:
    return _BRIDGE_PACKAGE_DIR / _BRIDGE_DLL_RELATIVE


def _dotnet_binary() -> str:
    """Resolve the ``dotnet`` binary path.

    Preference order: ``SERENA_DOTNET`` env var, Homebrew cask path under
    ``/opt/homebrew`` or ``/usr/local``, Microsoft installer path under
    ``/usr/local/share/dotnet``, then bare ``dotnet`` on PATH.
    """
    override = os.environ.get("SERENA_DOTNET")
    if override:
        if Path(override).exists():
            return override
    for candidate in (
        Path("/opt/homebrew/bin/dotnet"),
        Path("/usr/local/bin/dotnet"),
        Path("/usr/local/share/dotnet/dotnet"),
    ):
        if candidate.exists():
            return str(candidate)
    which = shutil.which("dotnet")
    if which:
        return which
    return "dotnet"


def _build_bridge_if_needed() -> Path:
    """Compile the C# bridge the first time the backend is used.

    Mirrors the Java / Rust / Go / Swift bridges' lazy-build pattern: if
    the dll is absent, run ``dotnet build --configuration Release`` under
    a file lock so concurrent first-use calls do not race. The dotnet SDK
    handles dependency resolution (Microsoft.CodeAnalysis.CSharp) via
    NuGet.
    """
    dll = _bridge_dll_path()
    if dll.exists():
        return dll
    _BRIDGE_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _BRIDGE_PACKAGE_DIR / ".build.lock"
    with lock_path.open("w") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        if dll.exists():
            return dll
        result = subprocess.run(
            [_dotnet_binary(), "build", "--configuration", "Release", "--nologo"],
            check=False,
            cwd=str(_BRIDGE_PACKAGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ParseError(
                language_key="csharp",
                source_preview="",
                detail=f"failed to build serena-csharp-bridge:\n{result.stdout}\n{result.stderr}",
            )
    if not dll.exists():
        raise ParseError(
            language_key="csharp",
            source_preview="",
            detail=f"dotnet build reported success but dll is missing at {dll}",
        )
    return dll


class _CSharpBridge:
    """Long-lived ``serena-csharp-bridge`` subprocess.

    One bridge is shared by a single :class:`CSharpStructuralLanguage`
    instance. Calls are serialized by a mutex so concurrent Python callers
    do not interleave their JSON frames.
    """

    def __init__(self, dll_path: Path):
        self._dll_path = dll_path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                [_dotnet_binary(), "exec", str(self._dll_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        return self._process

    def call(self, op: str, **payload: Any) -> dict[str, Any]:
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
                    language_key="csharp",
                    source_preview="",
                    detail=f"bridge communication failed: {exc}; stderr:\n{stderr}",
                )
            return json.loads(response_bytes)

    def close(self) -> None:
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
# Declaration renderer helpers
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
    if value is None:
        return ""
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


def _ensure_trailing_newline(src: str) -> str:
    return src if src[-1:] == "\n" else src + "\n"


def _generics_clause(generics: str | None) -> str:
    return f"<{generics}>" if generics else ""


def _prefix_tokens(attributes: str | None, modifiers: str | None) -> str:
    parts: list[str] = []
    if attributes:
        parts.append(attributes)
    if modifiers:
        parts.append(modifiers)
    joined = " ".join(p for p in parts if p)
    return f"{joined} " if joined else ""


_CSHARP_MODIFIERS = frozenset(
    {
        "public",
        "private",
        "protected",
        "internal",
        "static",
        "readonly",
        "abstract",
        "sealed",
        "virtual",
        "override",
        "new",
        "partial",
        "async",
        "extern",
        "unsafe",
        "volatile",
        "ref",
        "required",
        "file",
        "fixed",
        "const",
        "event",
        "in",
        "out",
        "implicit",
        "explicit",
        "operator",
    }
)


def _require_starts_with(statement: str, keyword: str, kind: KindName) -> None:
    """Require the first non-whitespace, non-modifier token to be ``keyword``.

    C# attributes (``[Foo]``, ``[Foo(arg)]``) and modifiers may precede
    the keyword. ``global`` and ``record`` are handled specially: a
    ``using`` declaration may be prefixed by ``global`` / ``global using``;
    a ``record`` declaration may be ``record``, ``record class``, or
    ``record struct`` -- the first identifier token after modifiers must
    be exactly ``keyword``.
    """
    stripped = statement.lstrip()
    while stripped:
        # strip attribute lists [Foo] / [Foo(args)]
        if stripped.startswith("["):
            depth = 1
            idx = 1
            while idx < len(stripped) and depth > 0:
                if stripped[idx] == "[":
                    depth += 1
                elif stripped[idx] == "]":
                    depth -= 1
                idx += 1
            stripped = stripped[idx:].lstrip()
            continue
        # strip modifiers
        head, _, rest = stripped.partition(" ")
        head = head.strip()
        # special-case: `global using` -- "global" is a contextual prefix for using decls
        if head == "global" and keyword == "using":
            stripped = rest.lstrip()
            continue
        if head in _CSHARP_MODIFIERS:
            stripped = rest.lstrip()
            continue
        break
    head_token = stripped.split(None, 1)
    if not head_token or head_token[0] != keyword:
        raise DeclarationError(kind, f"{kind} statement must start with {keyword!r}")


# -----------------------------------------------------------------------------
# Pattern rendering (replacement templating)
# -----------------------------------------------------------------------------


_CAPTURE_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)")


def _render_replacement_template(replacement_source: str, bindings: Mapping[str, Any]) -> str:
    """Substitute ``$name`` references in ``replacement_source`` with
    ``bindings[name]`` converted to source text. Bindings may be raw strings
    or :class:`_CSharpDeclaration` handles.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise PatternError(reason="binding", detail=f"no binding for ${name}")
        value = bindings[name]
        if isinstance(value, _CSharpDeclaration):
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


class CSharpStructuralLanguage(StructuralLanguage):
    """C# structural-language backend driven by Roslyn via subprocess.

    One backend instance owns one long-lived bridge subprocess. Disposing
    the instance (``close()`` or GC) tears the subprocess down cleanly.
    """

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        dll_path: Path | None = None,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`CSharpLogicalNameResolver` rooted at CWD.
        :param dll_path: explicit path to the bridge dll. If ``None``,
            uses the bundled bridge built lazily on first use.
        """
        self._name_resolver = name_resolver or CSharpLogicalNameResolver(Path.cwd())
        self._explicit_dll = dll_path
        self._bridge: _CSharpBridge | None = None

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "csharp"

    @property
    def kind_schema(self) -> KindSchema:
        return _CSHARP_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- bridge access -----------------------------------------------------

    def _get_bridge(self) -> _CSharpBridge:
        if self._bridge is None:
            dll = self._explicit_dll or _build_bridge_if_needed()
            self._bridge = _CSharpBridge(dll)
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

    def parse(self, source: str) -> _CSharpTree:
        return _CSharpTree(source=source)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _CSharpTree):
            return tree.source
        if isinstance(tree, _CSharpDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _CSharpTree):
            raise TypeError(f"root_kind expects a _CSharpTree; got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _CSharpTree):
            raise TypeError(f"walk_symbols expects a _CSharpTree; got {type(tree).__name__}")
        response = self._get_bridge().call("walk_symbols", source=tree.source)
        _raise_for_error(response)
        for entry in response.get("symbols", []):
            body = entry.get("body_range")
            ref = _CSharpSymbolRef(
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
    ) -> _CSharpDeclaration:
        self.kind_schema.get(kind)
        children_list = list(children)

        for child in children_list:
            if not isinstance(child, _CSharpDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _CSharpDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind == "using":
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, "using", kind)
            return _CSharpDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "namespace":
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, "namespace", kind)
            return _CSharpDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind in {"class", "struct", "interface", "enum", "record", "delegate"}:
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, kind, kind)
            return _CSharpDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "field":
            statement = _require_str(attributes, "statement", kind)
            return _CSharpDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "property":
            statement = _require_str(attributes, "statement", kind)
            return _CSharpDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "event":
            statement = _require_str(attributes, "statement", kind)
            return _CSharpDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "method":
            return self._build_method(kind, attributes)

        if kind == "constructor":
            return self._build_constructor(kind, attributes)

        raise DeclarationError(kind, f"unsupported kind in C# backend: {kind!r}")

    def _build_method(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
    ) -> _CSharpDeclaration:
        name = _require_str(attributes, "name", kind)
        params = _optional_str(attributes, "parameters", kind)
        return_type = _optional_str(attributes, "return_type", kind) or "void"
        body = _optional_str(attributes, "body", kind)
        generics = _optional_str_or_none(attributes, "generics", kind)
        modifiers = _optional_str_or_none(attributes, "modifiers", kind)
        cs_attrs = _optional_str_or_none(attributes, "attributes", kind)
        body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
        generic_clause = _generics_clause(generics)
        rendered = f"{_prefix_tokens(cs_attrs, modifiers)}{return_type} {name}{generic_clause}({params}) {{\n{body_inner}}}\n"
        return _CSharpDeclaration(kind=kind, source=rendered)

    def _build_constructor(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
    ) -> _CSharpDeclaration:
        name = _require_str(attributes, "name", kind)
        params = _optional_str(attributes, "parameters", kind)
        body = _optional_str(attributes, "body", kind)
        modifiers = _optional_str_or_none(attributes, "modifiers", kind)
        cs_attrs = _optional_str_or_none(attributes, "attributes", kind)
        body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
        rendered = f"{_prefix_tokens(cs_attrs, modifiers)}{name}({params}) {{\n{body_inner}}}\n"
        return _CSharpDeclaration(kind=kind, source=rendered)

    # ---- mutation ----------------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _CSharpTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _CSharpDeclaration):
            raise TypeError(f"child must be a _CSharpDeclaration; got {type(child).__name__}")

        tree, parent_path = self._resolve_insertion_parent(parent)
        anchor_path = anchor.name_path if isinstance(anchor, _CSharpSymbolRef) else None
        response = self._get_bridge().call(
            "insert_child",
            source=tree.source,
            parent_name_path=parent_path,
            anchor_name_path=anchor_path,
            position=position,
            child_source=child.source,
        )
        _raise_for_error(response)
        return _CSharpTree(source=response["source"])

    def remove_child(self, parent: Any, child: Any) -> _CSharpTree:
        if not isinstance(child, _CSharpSymbolRef):
            raise TypeError(f"child must be a _CSharpSymbolRef; got {type(child).__name__}")
        tree, _ = self._resolve_insertion_parent(parent)
        response = self._get_bridge().call(
            "remove_child",
            source=tree.source,
            child_name_path=child.name_path,
        )
        _raise_for_error(response)
        return _CSharpTree(source=response["source"])

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_CSharpTree, str | None]:
        """Normalize ``parent`` to ``(tree, parent_name_path_or_None)``.

        Accepts only a whole tree -- nested insertion via a bare
        :class:`_CSharpSymbolRef` is not supported in v1 (no body ranges
        are exposed), matching the Java / Swift / Go / Rust backend
        contract. Adding a method to an existing class requires
        rebuilding the class block.
        """
        if isinstance(parent, _CSharpTree):
            return parent, None
        raise TypeError(
            f"parent must be a _CSharpTree; got {type(parent).__name__}. Pass the tree and use anchor / position to target a nested symbol."
        )

    # ---- pattern matching --------------------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError(reason="parse", detail="empty pattern")
        return _CSharpPattern(source=pattern_source)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _CSharpTree):
            raise TypeError(f"find_matches expects a _CSharpTree; got {type(tree).__name__}")
        if not isinstance(pattern, _CSharpPattern):
            raise TypeError(f"pattern must be a _CSharpPattern; got {type(pattern).__name__}")
        scope_path: str | None = None
        if isinstance(scope, _CSharpSymbolRef):
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
            node_ref = _CSharpSymbolRef(
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
    ) -> _CSharpDeclaration:
        if not replacement_source:
            raise PatternError(reason="parse", detail="empty replacement")
        rendered = _render_replacement_template(replacement_source, bindings)
        return _CSharpDeclaration(kind="_replacement", source=rendered)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _CSharpTree:
        if not isinstance(tree, _CSharpTree):
            raise TypeError(f"apply_replacement expects a _CSharpTree; got {type(tree).__name__}")
        if not isinstance(replacement, _CSharpDeclaration):
            raise TypeError(f"replacement must be a _CSharpDeclaration; got {type(replacement).__name__}")
        if not isinstance(match.node, _CSharpSymbolRef):
            raise TypeError(f"match.node must be a _CSharpSymbolRef; got {type(match.node).__name__}")
        response = self._get_bridge().call(
            "apply_replacement",
            source=tree.source,
            match_offset=match.node.extent_offset,
            match_length=match.node.extent_length,
            replacement_source=replacement.source,
        )
        _raise_for_error(response)
        return _CSharpTree(source=response["source"])

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _CSharpTree:
        if source_kind not in self.kind_schema.source_kinds:
            raise DeclarationError(
                source_kind,
                f"not a source kind in {self.language_key}; valid: {sorted(self.kind_schema.source_kinds)}",
            )
        return _CSharpTree(source="")


def _raise_for_error(response: Mapping[str, Any]) -> None:
    """Map a bridge error response onto the structural error hierarchy."""
    if response.get("ok", False):
        return
    kind = str(response.get("error_kind", "bridge"))
    message = str(response.get("message", ""))
    if kind == "pattern_parse":
        raise PatternError(reason="parse", detail=message)
    if kind in {"symbol_missing", "no_body", "bad_request"}:
        raise ValueError(f"csharp bridge: {message}")
    if kind == "unknown_op":
        raise RuntimeError(f"csharp bridge: {message}")
    raise ParseError(language_key="csharp", source_preview="", detail=f"{kind}: {message}")
