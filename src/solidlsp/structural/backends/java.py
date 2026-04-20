"""Java structural backend.

Bridges :class:`~solidlsp.structural.base.StructuralLanguage` to Java's
``javaparser-core`` library, which runs as a long-lived subprocess jar
(see :mod:`java_bridge`).

The handle shape mirrors the Swift / TypeScript / Go / Rust backends:
parse wraps source verbatim, every mutation applies a text-level edit and
re-parses via the subprocess, and symbols are value-typed refs (kind +
name_path + byte offsets) that survive across re-parses. Byte offsets on
the wire are UTF-8 byte offsets, computed in the bridge from JavaParser's
(line, column) positions.
"""

from __future__ import annotations

import json
import os
import re
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
    description="The full Java statement text, rendered verbatim.",
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
    description="Return-type expression (e.g. ``void``, ``int``, ``List<String>``).",
)

_ATTR_GENERICS = AttributeSpec(
    name="generics",
    type_hint="str",
    required=False,
    description="Generic parameter clause without angle brackets (e.g. ``T, U extends Cloneable``).",
)

_ATTR_THROWS = AttributeSpec(
    name="throws",
    type_hint="str",
    required=False,
    description="Throws clause body without the leading ``throws`` keyword.",
)

_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=False,
    description="Function / method body text placed inside the braces.",
)

_ATTR_MODIFIERS = AttributeSpec(
    name="modifiers",
    type_hint="str",
    required=False,
    description="Modifier tokens placed before the item (e.g. ``public``, ``public static``, ``private final``).",
)

_ATTR_ANNOTATIONS = AttributeSpec(
    name="annotations",
    type_hint="str",
    required=False,
    description="Annotations text placed before modifiers (e.g. ``@Override``).",
)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


def java_kind_schema() -> KindSchema:
    """Return the :class:`KindSchema` exposed by the Java backend.

    The vocabulary captures top-level constructs (package, import, class,
    interface, enum, record, annotation_type) plus the three major member
    kinds (method, field, constructor) for in-body navigation. Nested
    types are walked but not addressable as insertion parents in v1 —
    mirroring the Rust backend's "top-level + one layer of walk" stance.
    Members have empty ``allowed_parent_kinds`` because their containing
    types do not expose body ranges in v1; to add a member to an existing
    type, agents rebuild the type block.
    """
    source_file = KindSpec(
        name="source_file",
        description="A Java compilation unit; the root of any parsed tree.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset({"package", "import", "class", "interface", "enum", "record", "annotation_type"}),
    )
    package_item = KindSpec(
        name="package",
        description="A Java ``package`` declaration (at most one per file).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    import_item = KindSpec(
        name="import",
        description="A Java ``import`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    class_item = KindSpec(
        name="class",
        description="A Java ``class`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    interface_item = KindSpec(
        name="interface",
        description="A Java ``interface`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    enum_item = KindSpec(
        name="enum",
        description="A Java ``enum`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    record_item = KindSpec(
        name="record",
        description="A Java ``record`` declaration (Java 14+).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    annotation_item = KindSpec(
        name="annotation_type",
        description="A Java annotation-interface (``@interface``) declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    method_item = KindSpec(
        name="method",
        description=(
            "A Java method inside a class / interface / enum / record. Walked and "
            "removable in v1; inserting a method into an existing type requires "
            "rebuilding the type because type body ranges are not exposed."
        ),
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_GENERICS,
            _ATTR_THROWS,
            _ATTR_MODIFIERS,
            _ATTR_ANNOTATIONS,
        ),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    field_item = KindSpec(
        name="field",
        description=(
            "A Java field declaration inside a class / interface / enum. "
            "Walked and removable in v1; inserting a field into an existing "
            "type requires rebuilding the type."
        ),
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    constructor_item = KindSpec(
        name="constructor",
        description=(
            "A Java constructor inside a class / enum / record. Walked and "
            "removable in v1; inserting a constructor into an existing "
            "type requires rebuilding the type."
        ),
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_BODY,
            _ATTR_GENERICS,
            _ATTR_THROWS,
            _ATTR_MODIFIERS,
            _ATTR_ANNOTATIONS,
        ),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )

    specs = (
        source_file,
        package_item,
        import_item,
        class_item,
        interface_item,
        enum_item,
        record_item,
        annotation_item,
        method_item,
        field_item,
        constructor_item,
    )
    return KindSchema(
        language_key="java",
        source_kinds=frozenset({"source_file"}),
        kinds={s.name: s for s in specs},
    )


_JAVA_KIND_SCHEMA = java_kind_schema()


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


_JAVA_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z_$0-9]*$")


class JavaLogicalNameResolver:
    """Java logical-name resolver.

    Logical names are dotted package paths (``com.example.foo``) that map
    to ``<project_root>/com/example/foo.java``. Each segment must be a
    valid Java identifier. File-level resolver — intra-file navigation is
    :meth:`JavaStructuralLanguage.walk_symbols`' job, not the resolver's.
    """

    def __init__(self, project_root: Path):
        self._project_root = project_root

    def parse(self, raw: str) -> LogicalName:
        if not raw:
            raise NameResolutionError(raw, "java logical name must be non-empty")
        parts = tuple(raw.split("."))
        for segment in parts:
            if not _JAVA_IDENTIFIER_RE.match(segment):
                raise NameResolutionError(
                    raw,
                    f"invalid identifier segment: {segment!r}",
                )
        return LogicalName(raw=raw, parts=parts)

    def resolve(self, name: LogicalName) -> NameResolution:
        if not name.parts:
            raise NameResolutionError(name.raw, "java logical name must have at least one segment")
        *dirs, last = name.parts
        candidate = self._project_root.joinpath(*dirs, f"{last}.java")
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
class _JavaTree:
    """Opaque handle for a parsed Java source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    """

    source: str


@dataclass(frozen=True)
class _JavaSymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    :ivar kind: structural kind name.
    :ivar name_path: ``Parent/Child`` path within the source file.
    :ivar extent_offset: start byte of the symbol's declaration.
    :ivar extent_length: byte length of the symbol's declaration.
    :ivar body_range: always ``None`` in v1 — Java body ranges are not
        exposed (no nested-symbol insertion into existing types).
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _JavaDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text, ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _JavaPattern:
    """A compiled Java pattern.

    :ivar source: pattern source text with ``$name`` / ``$_`` sigils.
    """

    source: str


# -----------------------------------------------------------------------------
# Subprocess bridge
# -----------------------------------------------------------------------------

_BRIDGE_PACKAGE_DIR = Path(__file__).resolve().parent / "java_bridge"
_BRIDGE_JAR_RELATIVE = Path("target") / "serena-java-bridge.jar"


def _bridge_jar_path() -> Path:
    return _BRIDGE_PACKAGE_DIR / _BRIDGE_JAR_RELATIVE


def _java_binary() -> str:
    """Resolve the ``java`` binary path.

    Preference order: ``JAVA_HOME/bin/java``, Homebrew default prefixes,
    ``/usr/libexec/java_home``, then bare ``java`` on PATH. Mirrors what
    Maven itself does internally, so the runtime uses the same JDK that
    ``mvn package`` just used.
    """
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "java"
        if candidate.exists():
            return str(candidate)
    for prefix in (Path("/opt/homebrew/opt/openjdk"), Path("/usr/local/opt/openjdk")):
        candidate = prefix / "bin" / "java"
        if candidate.exists():
            return str(candidate)
    try:
        home = subprocess.check_output(["/usr/libexec/java_home"], text=True, stderr=subprocess.DEVNULL).strip()
        if home:
            candidate = Path(home) / "bin" / "java"
            if candidate.exists():
                return str(candidate)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return "java"


def _build_bridge_if_needed() -> Path:
    """Compile the Java bridge the first time the backend is used.

    Mirrors the Swift / Rust / Go bridges' lazy-build pattern: if the jar
    is absent, run ``mvn package`` under a file lock so concurrent
    first-use calls do not race. Maven handles dependency resolution
    (javaparser-core, gson) via Maven Central.
    """
    jar = _bridge_jar_path()
    if jar.exists():
        return jar
    _BRIDGE_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _BRIDGE_PACKAGE_DIR / ".build.lock"
    with lock_path.open("w") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        if jar.exists():
            return jar
        result = subprocess.run(
            ["mvn", "-q", "-B", "package"],
            check=False,
            cwd=str(_BRIDGE_PACKAGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ParseError(
                language_key="java",
                source_preview="",
                detail=f"failed to build serena-java-bridge:\n{result.stdout}\n{result.stderr}",
            )
    if not jar.exists():
        raise ParseError(
            language_key="java",
            source_preview="",
            detail=f"mvn package reported success but jar is missing at {jar}",
        )
    return jar


class _JavaBridge:
    """Long-lived ``serena-java-bridge`` subprocess.

    One bridge is shared by a single :class:`JavaStructuralLanguage`
    instance. Calls are serialized by a mutex so concurrent Python callers
    do not interleave their JSON frames.
    """

    def __init__(self, jar_path: Path):
        self._jar_path = jar_path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                [_java_binary(), "-jar", str(self._jar_path)],
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
                    language_key="java",
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


def _throws_clause(throws: str | None) -> str:
    return f" throws {throws}" if throws else ""


def _prefix_tokens(annotations: str | None, modifiers: str | None) -> str:
    parts: list[str] = []
    if annotations:
        parts.append(annotations)
    if modifiers:
        parts.append(modifiers)
    joined = " ".join(p for p in parts if p)
    return f"{joined} " if joined else ""


def _require_starts_with(statement: str, keyword: str, kind: KindName) -> None:
    """Require the first non-whitespace, non-modifier token to be ``keyword``.

    Java modifiers (``public`` / ``private`` / ``protected`` / ``static`` /
    ``final`` / ``abstract`` / ``sealed`` / ``non-sealed`` / ``default`` /
    ``strictfp``) may precede the keyword, as may annotations ``@Foo``.
    """
    modifiers = {
        "public",
        "private",
        "protected",
        "static",
        "final",
        "abstract",
        "sealed",
        "non-sealed",
        "default",
        "strictfp",
        "native",
        "synchronized",
        "transient",
        "volatile",
    }
    stripped = statement.lstrip()
    while stripped:
        # strip annotations @Foo(...) or @Foo, but not the @interface keyword
        if stripped.startswith("@") and not stripped.startswith("@interface"):
            idx = 1
            # identifier
            while idx < len(stripped) and (stripped[idx].isalnum() or stripped[idx] in "_.$"):
                idx += 1
            # optional args
            if idx < len(stripped) and stripped[idx] == "(":
                depth = 1
                idx += 1
                while idx < len(stripped) and depth > 0:
                    if stripped[idx] == "(":
                        depth += 1
                    elif stripped[idx] == ")":
                        depth -= 1
                    idx += 1
            stripped = stripped[idx:].lstrip()
            continue
        head, _, rest = stripped.partition(" ")
        head = head.strip()
        if head in modifiers:
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
    or :class:`_JavaDeclaration` handles.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise PatternError(reason="binding", detail=f"no binding for ${name}")
        value = bindings[name]
        if isinstance(value, _JavaDeclaration):
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


class JavaStructuralLanguage(StructuralLanguage):
    """Java structural-language backend driven by ``javaparser-core`` via subprocess.

    One backend instance owns one long-lived bridge subprocess. Disposing
    the instance (``close()`` or GC) tears the subprocess down cleanly.
    """

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        jar_path: Path | None = None,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`JavaLogicalNameResolver` rooted at CWD.
        :param jar_path: explicit path to the bridge jar. If ``None``,
            uses the bundled bridge built lazily on first use.
        """
        self._name_resolver = name_resolver or JavaLogicalNameResolver(Path.cwd())
        self._explicit_jar = jar_path
        self._bridge: _JavaBridge | None = None

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "java"

    @property
    def kind_schema(self) -> KindSchema:
        return _JAVA_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- bridge access -----------------------------------------------------

    def _get_bridge(self) -> _JavaBridge:
        if self._bridge is None:
            jar = self._explicit_jar or _build_bridge_if_needed()
            self._bridge = _JavaBridge(jar)
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

    def parse(self, source: str) -> _JavaTree:
        return _JavaTree(source=source)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _JavaTree):
            return tree.source
        if isinstance(tree, _JavaDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _JavaTree):
            raise TypeError(f"root_kind expects a _JavaTree; got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _JavaTree):
            raise TypeError(f"walk_symbols expects a _JavaTree; got {type(tree).__name__}")
        response = self._get_bridge().call("walk_symbols", source=tree.source)
        _raise_for_error(response)
        for entry in response.get("symbols", []):
            body = entry.get("body_range")
            ref = _JavaSymbolRef(
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
    ) -> _JavaDeclaration:
        self.kind_schema.get(kind)
        children_list = list(children)

        for child in children_list:
            if not isinstance(child, _JavaDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _JavaDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind == "package":
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, "package", kind)
            return _JavaDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "import":
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, "import", kind)
            return _JavaDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind in {"class", "interface", "enum", "record", "annotation_type"}:
            statement = _require_str(attributes, "statement", kind)
            keyword = kind if kind != "annotation_type" else "@interface"
            _require_starts_with(statement, keyword, kind)
            return _JavaDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "field":
            statement = _require_str(attributes, "statement", kind)
            return _JavaDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "method":
            return self._build_method(kind, attributes)

        if kind == "constructor":
            return self._build_constructor(kind, attributes)

        raise DeclarationError(kind, f"unsupported kind in Java backend: {kind!r}")

    def _build_method(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
    ) -> _JavaDeclaration:
        name = _require_str(attributes, "name", kind)
        params = _optional_str(attributes, "parameters", kind)
        return_type = _optional_str(attributes, "return_type", kind) or "void"
        body = _optional_str(attributes, "body", kind)
        generics = _optional_str_or_none(attributes, "generics", kind)
        throws_clause = _optional_str_or_none(attributes, "throws", kind)
        modifiers = _optional_str_or_none(attributes, "modifiers", kind)
        annotations = _optional_str_or_none(attributes, "annotations", kind)
        body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
        generic_clause = _generics_clause(generics)
        if generic_clause:
            generic_clause = generic_clause + " "
        rendered = (
            f"{_prefix_tokens(annotations, modifiers)}{generic_clause}{return_type} {name}"
            f"({params})"
            f"{_throws_clause(throws_clause)}"
            f" {{\n{body_inner}}}\n"
        )
        return _JavaDeclaration(kind=kind, source=rendered)

    def _build_constructor(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
    ) -> _JavaDeclaration:
        name = _require_str(attributes, "name", kind)
        params = _optional_str(attributes, "parameters", kind)
        body = _optional_str(attributes, "body", kind)
        generics = _optional_str_or_none(attributes, "generics", kind)
        throws_clause = _optional_str_or_none(attributes, "throws", kind)
        modifiers = _optional_str_or_none(attributes, "modifiers", kind)
        annotations = _optional_str_or_none(attributes, "annotations", kind)
        body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
        generic_clause = _generics_clause(generics)
        if generic_clause:
            generic_clause = generic_clause + " "
        rendered = (
            f"{_prefix_tokens(annotations, modifiers)}{generic_clause}{name}({params}){_throws_clause(throws_clause)} {{\n{body_inner}}}\n"
        )
        return _JavaDeclaration(kind=kind, source=rendered)

    # ---- mutation ----------------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _JavaTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _JavaDeclaration):
            raise TypeError(f"child must be a _JavaDeclaration; got {type(child).__name__}")

        tree, parent_path = self._resolve_insertion_parent(parent)
        anchor_path = anchor.name_path if isinstance(anchor, _JavaSymbolRef) else None
        response = self._get_bridge().call(
            "insert_child",
            source=tree.source,
            parent_name_path=parent_path,
            anchor_name_path=anchor_path,
            position=position,
            child_source=child.source,
        )
        _raise_for_error(response)
        return _JavaTree(source=response["source"])

    def remove_child(self, parent: Any, child: Any) -> _JavaTree:
        if not isinstance(child, _JavaSymbolRef):
            raise TypeError(f"child must be a _JavaSymbolRef; got {type(child).__name__}")
        tree, _ = self._resolve_insertion_parent(parent)
        response = self._get_bridge().call(
            "remove_child",
            source=tree.source,
            child_name_path=child.name_path,
        )
        _raise_for_error(response)
        return _JavaTree(source=response["source"])

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_JavaTree, str | None]:
        """Normalize ``parent`` to ``(tree, parent_name_path_or_None)``.

        Accepts only a whole tree — nested insertion via a bare
        :class:`_JavaSymbolRef` is not supported in v1 (no body ranges are
        exposed), matching the Swift / Go / Rust backend contract. In
        particular, adding a method to an existing class requires
        rebuilding the class block; it cannot be done as a sub-tree
        mutation.
        """
        if isinstance(parent, _JavaTree):
            return parent, None
        raise TypeError(
            f"parent must be a _JavaTree; got {type(parent).__name__}. Pass the tree and use anchor / position to target a nested symbol."
        )

    # ---- pattern matching --------------------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError(reason="parse", detail="empty pattern")
        return _JavaPattern(source=pattern_source)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _JavaTree):
            raise TypeError(f"find_matches expects a _JavaTree; got {type(tree).__name__}")
        if not isinstance(pattern, _JavaPattern):
            raise TypeError(f"pattern must be a _JavaPattern; got {type(pattern).__name__}")
        scope_path: str | None = None
        if isinstance(scope, _JavaSymbolRef):
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
            node_ref = _JavaSymbolRef(
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
    ) -> _JavaDeclaration:
        if not replacement_source:
            raise PatternError(reason="parse", detail="empty replacement")
        rendered = _render_replacement_template(replacement_source, bindings)
        return _JavaDeclaration(kind="_replacement", source=rendered)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _JavaTree:
        if not isinstance(tree, _JavaTree):
            raise TypeError(f"apply_replacement expects a _JavaTree; got {type(tree).__name__}")
        if not isinstance(replacement, _JavaDeclaration):
            raise TypeError(f"replacement must be a _JavaDeclaration; got {type(replacement).__name__}")
        if not isinstance(match.node, _JavaSymbolRef):
            raise TypeError(f"match.node must be a _JavaSymbolRef; got {type(match.node).__name__}")
        response = self._get_bridge().call(
            "apply_replacement",
            source=tree.source,
            match_offset=match.node.extent_offset,
            match_length=match.node.extent_length,
            replacement_source=replacement.source,
        )
        _raise_for_error(response)
        return _JavaTree(source=response["source"])

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _JavaTree:
        if source_kind not in self.kind_schema.source_kinds:
            raise DeclarationError(
                source_kind,
                f"not a source kind in {self.language_key}; valid: {sorted(self.kind_schema.source_kinds)}",
            )
        return _JavaTree(source="")


def _raise_for_error(response: Mapping[str, Any]) -> None:
    """Map a bridge error response onto the structural error hierarchy."""
    if response.get("ok", False):
        return
    kind = str(response.get("error_kind", "bridge"))
    message = str(response.get("message", ""))
    if kind == "pattern_parse":
        raise PatternError(reason="parse", detail=message)
    if kind in {"symbol_missing", "no_body", "bad_request"}:
        raise ValueError(f"java bridge: {message}")
    if kind == "unknown_op":
        raise RuntimeError(f"java bridge: {message}")
    raise ParseError(language_key="java", source_preview="", detail=f"{kind}: {message}")
