"""TypeScript structural backend.

Bridges :class:`~solidlsp.structural.base.StructuralLanguage` to the
TypeScript compiler API, which runs as a long-lived Node.js subprocess
(see :mod:`typescript_bridge`).

The handle shape mirrors the Swift backend: parse wraps source verbatim,
every mutation applies a text-level edit and re-parses via the subprocess,
and symbols are value-typed refs (kind + name_path + byte offsets) that
survive across re-parses. Byte offsets in the wire protocol are UTF-8 byte
offsets, converted from TypeScript's UTF-16 positions inside the bridge.
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
    description="The full TypeScript statement text, rendered verbatim.",
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
    description="Return type expression (without ``:``).",
)

_ATTR_TYPE_PARAMETERS = AttributeSpec(
    name="type_parameters",
    type_hint="str",
    required=False,
    description="Generic parameter clause without angle brackets (e.g. ``T, U extends P``).",
)

_ATTR_EXTENDS = AttributeSpec(
    name="extends",
    type_hint="list[str]",
    required=False,
    description="Base class / interface list appended after ``extends``.",
)

_ATTR_IMPLEMENTS = AttributeSpec(
    name="implements",
    type_hint="list[str]",
    required=False,
    description="Interface list appended after ``implements`` on a class.",
)

_ATTR_MODIFIERS = AttributeSpec(
    name="modifiers",
    type_hint="str",
    required=False,
    description="Modifier tokens placed before the decl keyword (``export``, ``public static``, ``async``, etc.).",
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

_ATTR_QUESTION = AttributeSpec(
    name="optional",
    type_hint="bool",
    required=False,
    description="If true, append ``?`` after the name (interface/class member optionality).",
)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


def typescript_kind_schema() -> KindSchema:
    """Return the :class:`KindSchema` exposed by the TypeScript backend."""
    source_file = KindSpec(
        name="source_file",
        description="A TypeScript source file; the root of any parsed tree.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(
            {
                "import",
                "export",
                "class",
                "interface",
                "type_alias",
                "enum",
                "namespace",
                "function",
                "variable",
            }
        ),
    )
    imp = KindSpec(
        name="import",
        description="A TypeScript ``import`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file", "namespace"}),
        allowed_child_kinds=frozenset(),
    )
    exp = KindSpec(
        name="export",
        description="A bare ``export`` declaration (re-exports, ``export default X``, ``export * from``).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file", "namespace"}),
        allowed_child_kinds=frozenset(),
    )
    type_parents = frozenset({"source_file", "namespace"})
    namespace_children = frozenset(
        {
            "import",
            "export",
            "class",
            "interface",
            "type_alias",
            "enum",
            "namespace",
            "function",
            "variable",
        }
    )
    cls = KindSpec(
        name="class",
        description="A TypeScript ``class`` declaration.",
        attributes=(
            _ATTR_NAME,
            _ATTR_BODY,
            _ATTR_EXTENDS,
            _ATTR_IMPLEMENTS,
            _ATTR_MODIFIERS,
            _ATTR_TYPE_PARAMETERS,
        ),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=frozenset({"method", "constructor", "property"}),
    )
    iface = KindSpec(
        name="interface",
        description="A TypeScript ``interface`` declaration.",
        attributes=(
            _ATTR_NAME,
            _ATTR_BODY,
            _ATTR_EXTENDS,
            _ATTR_MODIFIERS,
            _ATTR_TYPE_PARAMETERS,
        ),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=frozenset({"method", "property"}),
    )
    ta = KindSpec(
        name="type_alias",
        description="A ``type X = ...;`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=frozenset(),
    )
    en = KindSpec(
        name="enum",
        description="A TypeScript ``enum`` declaration.",
        attributes=(_ATTR_NAME, _ATTR_BODY, _ATTR_MODIFIERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=frozenset({"enum_member"}),
    )
    ns = KindSpec(
        name="namespace",
        description="A TypeScript ``namespace`` / ``module`` block.",
        attributes=(_ATTR_NAME, _ATTR_BODY, _ATTR_MODIFIERS),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=namespace_children,
    )
    fn = KindSpec(
        name="function",
        description="A top-level function declaration.",
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_MODIFIERS,
            _ATTR_TYPE_PARAMETERS,
        ),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=frozenset(),
    )
    var = KindSpec(
        name="variable",
        description="A top-level ``const`` / ``let`` / ``var`` statement.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=type_parents,
        allowed_child_kinds=frozenset(),
    )
    method = KindSpec(
        name="method",
        description="A method declared inside a class or interface body.",
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_MODIFIERS,
            _ATTR_TYPE_PARAMETERS,
            _ATTR_QUESTION,
        ),
        allowed_parent_kinds=frozenset({"class", "interface"}),
        allowed_child_kinds=frozenset(),
    )
    ctor = KindSpec(
        name="constructor",
        description="A class ``constructor`` declaration.",
        attributes=(_ATTR_PARAMETERS, _ATTR_BODY, _ATTR_MODIFIERS),
        allowed_parent_kinds=frozenset({"class"}),
        allowed_child_kinds=frozenset(),
    )
    prop = KindSpec(
        name="property",
        description="A property inside a class or interface body.",
        attributes=(
            _ATTR_NAME,
            _ATTR_TYPE,
            _ATTR_INITIALIZER,
            _ATTR_MODIFIERS,
            _ATTR_QUESTION,
        ),
        allowed_parent_kinds=frozenset({"class", "interface"}),
        allowed_child_kinds=frozenset(),
    )
    enum_member = KindSpec(
        name="enum_member",
        description="A member declared inside an ``enum`` body.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"enum"}),
        allowed_child_kinds=frozenset(),
    )

    specs = (source_file, imp, exp, cls, iface, ta, en, ns, fn, var, method, ctor, prop, enum_member)
    return KindSchema(
        language_key="typescript",
        source_kinds=frozenset({"source_file"}),
        kinds={s.name: s for s in specs},
    )


_TS_KIND_SCHEMA = typescript_kind_schema()


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


_TS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z_$0-9]*$")


class TypeScriptLogicalNameResolver:
    """TypeScript logical-name resolver.

    Logical names are dotted module paths (``foo.bar.baz``) that map to
    ``<project_root>/foo/bar/baz.ts``. Each segment must be a valid
    TypeScript identifier.
    """

    def __init__(self, project_root: Path):
        """:param project_root: project root under which TypeScript sources live."""
        self._project_root = project_root

    def parse(self, raw: str) -> LogicalName:
        if not raw:
            raise NameResolutionError(raw, "typescript logical name must be non-empty")
        parts = tuple(raw.split("."))
        for segment in parts:
            if not _TS_IDENTIFIER_RE.match(segment):
                raise NameResolutionError(
                    raw,
                    f"invalid identifier segment: {segment!r}",
                )
        return LogicalName(raw=raw, parts=parts)

    def resolve(self, name: LogicalName) -> NameResolution:
        if not name.parts:
            raise NameResolutionError(name.raw, "typescript logical name must have at least one segment")
        *dirs, last = name.parts
        candidate = self._project_root.joinpath(*dirs, f"{last}.ts")
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
class _TsTree:
    """Opaque handle for a parsed TypeScript source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    """

    source: str


@dataclass(frozen=True)
class _TsSymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    Mirrors the Swift backend's symbol ref. Byte offsets let the symbol
    survive re-parse after a mutation that invalidates any AST objects.

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
class _TsDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text, ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _TsPattern:
    """A compiled TypeScript pattern.

    :ivar source: pattern source text with ``$name`` / ``$_`` sigils.
    """

    source: str


# -----------------------------------------------------------------------------
# Subprocess bridge
# -----------------------------------------------------------------------------

_BRIDGE_PACKAGE_DIR = Path(__file__).resolve().parent / "typescript_bridge"
_BRIDGE_ENTRY_RELATIVE = Path("bridge.mjs")
_BRIDGE_NODE_MODULES = _BRIDGE_PACKAGE_DIR / "node_modules"


def _bridge_entry_path() -> Path:
    """Absolute path to the bridge entry script."""
    return _BRIDGE_PACKAGE_DIR / _BRIDGE_ENTRY_RELATIVE


def _install_dependencies_if_needed() -> None:
    """Install node_modules the first time the bridge is used.

    Matches the Swift bridge's lazy-build pattern: if ``node_modules``
    is absent under the bridge package dir, run ``npm install --omit=dev``
    under a file lock so concurrent first-use calls don't race.
    """
    if _BRIDGE_NODE_MODULES.is_dir():
        return
    _BRIDGE_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _BRIDGE_PACKAGE_DIR / ".install.lock"
    with lock_path.open("w") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        if _BRIDGE_NODE_MODULES.is_dir():
            return
        result = subprocess.run(
            ["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
            check=False,
            cwd=str(_BRIDGE_PACKAGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ParseError(
                language_key="typescript",
                source_preview="",
                detail=f"failed to install serena-typescript-bridge dependencies:\n{result.stderr}",
            )
    if not _BRIDGE_NODE_MODULES.is_dir():
        raise ParseError(
            language_key="typescript",
            source_preview="",
            detail=f"npm install reported success but node_modules is missing at {_BRIDGE_NODE_MODULES}",
        )


class _TsBridge:
    """Long-lived ``node bridge.mjs`` subprocess.

    One bridge is shared by a single :class:`TypeScriptStructuralLanguage`
    instance. Calls are serialized by a mutex so concurrent Python callers
    do not interleave their JSON frames.
    """

    def __init__(self, entry_path: Path):
        """:param entry_path: absolute path to the bridge entry script."""
        self._entry_path = entry_path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        # lazy spawn; detect crashed / terminated children and respawn
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                ["node", str(self._entry_path)],
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
                    language_key="typescript",
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
    return src if src[-1:] == "\n" else src + "\n"


def _concat_children_source(children: Sequence[_TsDeclaration]) -> str:
    # join each child's source; callers insert inside a brace block
    return "".join(c.source for c in children)


def _join_bodies(body: str, children_source: str) -> str:
    parts = [body, children_source]
    joined = "".join(p for p in parts if p)
    if joined and joined[-1:] != "\n":
        joined += "\n"
    return joined


def _modifier_prefix(modifiers: str) -> str:
    return f"{modifiers} " if modifiers else ""


def _generic_clause(generic: str | None) -> str:
    return f"<{generic}>" if generic else ""


def _extends_clause(extends: list[str]) -> str:
    return f" extends {', '.join(extends)}" if extends else ""


def _implements_clause(implements: list[str]) -> str:
    return f" implements {', '.join(implements)}" if implements else ""


def _optional_marker(optional: bool) -> str:
    return "?" if optional else ""


# -----------------------------------------------------------------------------
# Pattern rendering (replacement templating)
# -----------------------------------------------------------------------------


_CAPTURE_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)")


def _render_replacement_template(replacement_source: str, bindings: Mapping[str, Any]) -> str:
    """Substitute ``$name`` references in ``replacement_source`` with
    ``bindings[name]`` converted to source text. Bindings may be raw strings
    or :class:`_TsDeclaration` handles.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise PatternError(reason="binding", detail=f"no binding for ${name}")
        value = bindings[name]
        if isinstance(value, _TsDeclaration):
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


class TypeScriptStructuralLanguage(StructuralLanguage):
    """TypeScript structural-language backend driven by the TS compiler via subprocess.

    One backend instance owns one long-lived bridge subprocess. Disposing
    the instance (``close()`` or GC) tears the subprocess down cleanly.
    """

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        entry_path: Path | None = None,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`TypeScriptLogicalNameResolver` rooted at CWD.
        :param entry_path: explicit path to the bridge entry script. If ``None``,
            uses the bundled ``typescript_bridge/bridge.mjs``.
        """
        self._name_resolver = name_resolver or TypeScriptLogicalNameResolver(Path.cwd())
        self._explicit_entry = entry_path
        self._bridge: _TsBridge | None = None

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "typescript"

    @property
    def kind_schema(self) -> KindSchema:
        return _TS_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- bridge access -----------------------------------------------------

    def _get_bridge(self) -> _TsBridge:
        if self._bridge is None:
            entry = self._explicit_entry or _bridge_entry_path()
            if self._explicit_entry is None:
                _install_dependencies_if_needed()
            self._bridge = _TsBridge(entry)
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

    def parse(self, source: str) -> _TsTree:
        # TypeScript parse is byte-identical by construction (we wrap
        # source verbatim); we skip the subprocess round-trip on parse so
        # construction of an opaque handle is cheap.
        return _TsTree(source=source)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _TsTree):
            return tree.source
        if isinstance(tree, _TsDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _TsTree):
            raise TypeError(f"root_kind expects a _TsTree; got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _TsTree):
            raise TypeError(f"walk_symbols expects a _TsTree; got {type(tree).__name__}")
        response = self._get_bridge().call("walk_symbols", source=tree.source)
        _raise_for_error(response)
        for entry in response.get("symbols", []):
            body = entry.get("body_range")
            ref = _TsSymbolRef(
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
    ) -> _TsDeclaration:
        self.kind_schema.get(kind)  # validate kind exists
        children_list = list(children)

        for child in children_list:
            if not isinstance(child, _TsDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _TsDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind == "import":
            statement = _require_str(attributes, "statement", kind)
            stripped = statement.lstrip()
            if not stripped.startswith(("import ", "import{")):
                raise DeclarationError(kind, "import statement must start with 'import'")
            return _TsDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "export":
            statement = _require_str(attributes, "statement", kind)
            stripped = statement.lstrip()
            if not stripped.startswith("export"):
                raise DeclarationError(kind, "export statement must start with 'export'")
            return _TsDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "type_alias":
            statement = _require_str(attributes, "statement", kind)
            stripped = statement.lstrip()
            head = stripped.split(None, 2)
            if not (head and (head[0] == "type" or (head[0] == "export" and len(head) > 1 and head[1] == "type"))):
                raise DeclarationError(kind, "type_alias statement must start with 'type' (optionally 'export type')")
            return _TsDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "variable":
            statement = _require_str(attributes, "statement", kind)
            stripped = statement.lstrip()
            head = stripped.split(None, 2)
            allowed_first = {"const", "let", "var"}
            if not head:
                raise DeclarationError(kind, "variable statement must be non-empty")
            if (
                head[0] in allowed_first
                or (head[0] == "export" and len(head) > 1 and head[1] in allowed_first)
                or (head[0] == "declare" and len(head) > 1 and head[1] in allowed_first)
            ):
                pass
            else:
                raise DeclarationError(kind, "variable statement must start with const/let/var")
            return _TsDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "enum_member":
            statement = _require_str(attributes, "statement", kind)
            return _TsDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind in {"class", "interface"}:
            name = _require_str(attributes, "name", kind)
            body = _optional_str(attributes, "body", kind)
            extends = _optional_str_list(attributes, "extends", kind)
            implements = _optional_str_list(attributes, "implements", kind) if kind == "class" else []
            modifiers = _optional_str(attributes, "modifiers", kind)
            generic = _optional_str_or_none(attributes, "type_parameters", kind)
            inner = _join_bodies(body, _concat_children_source(children_list))
            keyword = "class" if kind == "class" else "interface"
            rendered = (
                f"{_modifier_prefix(modifiers)}{keyword} {name}{_generic_clause(generic)}"
                f"{_extends_clause(extends)}{_implements_clause(implements)} {{\n{inner}}}\n"
            )
            return _TsDeclaration(kind=kind, source=rendered)

        if kind == "enum":
            name = _require_str(attributes, "name", kind)
            body = _optional_str(attributes, "body", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            inner = _join_bodies(body, _concat_children_source(children_list))
            rendered = f"{_modifier_prefix(modifiers)}enum {name} {{\n{inner}}}\n"
            return _TsDeclaration(kind=kind, source=rendered)

        if kind == "namespace":
            name = _require_str(attributes, "name", kind)
            body = _optional_str(attributes, "body", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            inner = _join_bodies(body, _concat_children_source(children_list))
            rendered = f"{_modifier_prefix(modifiers)}namespace {name} {{\n{inner}}}\n"
            return _TsDeclaration(kind=kind, source=rendered)

        if kind == "function":
            name = _require_str(attributes, "name", kind)
            params = _optional_str(attributes, "parameters", kind)
            ret = _optional_str_or_none(attributes, "return_type", kind)
            body = _optional_str(attributes, "body", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            generic = _optional_str_or_none(attributes, "type_parameters", kind)
            ret_clause = f": {ret}" if ret else ""
            body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
            rendered = f"{_modifier_prefix(modifiers)}function {name}{_generic_clause(generic)}({params}){ret_clause} {{\n{body_inner}}}\n"
            return _TsDeclaration(kind=kind, source=rendered)

        if kind == "method":
            name = _require_str(attributes, "name", kind)
            params = _optional_str(attributes, "parameters", kind)
            ret = _optional_str_or_none(attributes, "return_type", kind)
            method_body: str | None = _optional_str_or_none(attributes, "body", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            generic = _optional_str_or_none(attributes, "type_parameters", kind)
            optional = _optional_bool(attributes, "optional", kind)
            ret_clause = f": {ret}" if ret else ""
            if method_body is None:
                # interface-style method signature
                rendered = (
                    f"{_modifier_prefix(modifiers)}{name}{_optional_marker(optional)}{_generic_clause(generic)}({params}){ret_clause};\n"
                )
            else:
                method_body_inner = method_body if (not method_body or method_body[-1:] == "\n") else method_body + "\n"
                rendered = (
                    f"{_modifier_prefix(modifiers)}{name}{_optional_marker(optional)}"
                    f"{_generic_clause(generic)}({params}){ret_clause} {{\n{method_body_inner}}}\n"
                )
            return _TsDeclaration(kind=kind, source=rendered)

        if kind == "constructor":
            params = _optional_str(attributes, "parameters", kind)
            body = _optional_str(attributes, "body", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
            rendered = f"{_modifier_prefix(modifiers)}constructor({params}) {{\n{body_inner}}}\n"
            return _TsDeclaration(kind=kind, source=rendered)

        if kind == "property":
            name = _require_str(attributes, "name", kind)
            ty = _optional_str_or_none(attributes, "type", kind)
            init = _optional_str_or_none(attributes, "initializer", kind)
            modifiers = _optional_str(attributes, "modifiers", kind)
            optional = _optional_bool(attributes, "optional", kind)
            type_clause = f": {ty}" if ty else ""
            init_clause = f" = {init}" if init is not None else ""
            rendered = f"{_modifier_prefix(modifiers)}{name}{_optional_marker(optional)}{type_clause}{init_clause};\n"
            return _TsDeclaration(kind=kind, source=rendered)

        raise DeclarationError(kind, f"unsupported kind in TypeScript backend: {kind!r}")

    # ---- mutation ----------------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _TsTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _TsDeclaration):
            raise TypeError(f"child must be a _TsDeclaration; got {type(child).__name__}")

        tree, parent_path = self._resolve_insertion_parent(parent)
        anchor_path = anchor.name_path if isinstance(anchor, _TsSymbolRef) else None
        response = self._get_bridge().call(
            "insert_child",
            source=tree.source,
            parent_name_path=parent_path,
            anchor_name_path=anchor_path,
            position=position,
            child_source=child.source,
        )
        _raise_for_error(response)
        return _TsTree(source=response["source"])

    def remove_child(self, parent: Any, child: Any) -> _TsTree:
        if not isinstance(child, _TsSymbolRef):
            raise TypeError(f"child must be a _TsSymbolRef; got {type(child).__name__}")
        tree, _ = self._resolve_insertion_parent(parent)
        response = self._get_bridge().call(
            "remove_child",
            source=tree.source,
            child_name_path=child.name_path,
        )
        _raise_for_error(response)
        return _TsTree(source=response["source"])

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_TsTree, str | None]:
        """Normalize ``parent`` to ``(tree, parent_name_path_or_None)``.

        Accepts a whole tree (parent becomes None → root source file). A
        bare :class:`_TsSymbolRef` is not accepted — agents should pass the
        tree and use ``anchor`` for ref-based placement, matching the Swift
        backend's contract.
        """
        if isinstance(parent, _TsTree):
            return parent, None
        raise TypeError(
            f"parent must be a _TsTree; got {type(parent).__name__}. Pass the tree and use anchor / position to target a nested symbol."
        )

    # ---- pattern matching --------------------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError(reason="parse", detail="empty pattern")
        return _TsPattern(source=pattern_source)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _TsTree):
            raise TypeError(f"find_matches expects a _TsTree; got {type(tree).__name__}")
        if not isinstance(pattern, _TsPattern):
            raise TypeError(f"pattern must be a _TsPattern; got {type(pattern).__name__}")
        scope_path: str | None = None
        if isinstance(scope, _TsSymbolRef):
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
            node_ref = _TsSymbolRef(
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
    ) -> _TsDeclaration:
        if not replacement_source:
            raise PatternError(reason="parse", detail="empty replacement")
        rendered = _render_replacement_template(replacement_source, bindings)
        return _TsDeclaration(kind="_replacement", source=rendered)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _TsTree:
        if not isinstance(tree, _TsTree):
            raise TypeError(f"apply_replacement expects a _TsTree; got {type(tree).__name__}")
        if not isinstance(replacement, _TsDeclaration):
            raise TypeError(f"replacement must be a _TsDeclaration; got {type(replacement).__name__}")
        if not isinstance(match.node, _TsSymbolRef):
            raise TypeError(f"match.node must be a _TsSymbolRef; got {type(match.node).__name__}")
        response = self._get_bridge().call(
            "apply_replacement",
            source=tree.source,
            match_offset=match.node.extent_offset,
            match_length=match.node.extent_length,
            replacement_source=replacement.source,
        )
        _raise_for_error(response)
        return _TsTree(source=response["source"])

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _TsTree:
        if source_kind not in self.kind_schema.source_kinds:
            raise DeclarationError(
                source_kind,
                f"not a source kind in {self.language_key}; valid: {sorted(self.kind_schema.source_kinds)}",
            )
        return _TsTree(source="")


def _raise_for_error(response: Mapping[str, Any]) -> None:
    """Map a bridge error response onto the structural error hierarchy."""
    if response.get("ok", False):
        return
    kind = str(response.get("error_kind", "bridge"))
    message = str(response.get("message", ""))
    if kind == "pattern_parse":
        raise PatternError(reason="parse", detail=message)
    if kind in {"symbol_missing", "no_body", "bad_request"}:
        raise ValueError(f"typescript bridge: {message}")
    if kind == "unknown_op":
        raise RuntimeError(f"typescript bridge: {message}")
    raise ParseError(language_key="typescript", source_preview="", detail=f"{kind}: {message}")
