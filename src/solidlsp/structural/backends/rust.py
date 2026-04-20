"""Rust structural backend.

Bridges :class:`~solidlsp.structural.base.StructuralLanguage` to Rust's
``syn`` crate, which runs as a long-lived subprocess binary (see
:mod:`rust_bridge`).

The handle shape mirrors the Swift / TypeScript / Go backends: parse wraps
source verbatim, every mutation applies a text-level edit and re-parses via
the subprocess, and symbols are value-typed refs (kind + name_path + byte
offsets) that survive across re-parses. Byte offsets on the wire are
UTF-8 byte offsets directly from proc-macro2 spans.
"""

from __future__ import annotations

import json
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
    description="The full Rust statement text, rendered verbatim.",
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
    description="Return-type expression (without ``->``).",
)

_ATTR_GENERICS = AttributeSpec(
    name="generics",
    type_hint="str",
    required=False,
    description="Generic parameter clause without angle brackets (e.g. ``T, U: Clone``).",
)

_ATTR_WHERE = AttributeSpec(
    name="where_clause",
    type_hint="str",
    required=False,
    description="Where-clause body without the leading ``where`` keyword.",
)

_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=False,
    description="Function / method body text placed inside the braces.",
)

_ATTR_VISIBILITY = AttributeSpec(
    name="visibility",
    type_hint="str",
    required=False,
    description="Visibility modifier (``pub``, ``pub(crate)``, ...) placed before the item keyword.",
)

_ATTR_MODIFIERS = AttributeSpec(
    name="modifiers",
    type_hint="str",
    required=False,
    description="Extra modifier tokens placed between visibility and the item keyword (e.g. ``async unsafe``).",
)

_ATTR_SELF_TYPE = AttributeSpec(
    name="self_type",
    type_hint="str",
    required=True,
    description="Impl target type, rendered as source text (e.g. ``Point`` or ``Vec<T>``).",
)

_ATTR_TRAIT = AttributeSpec(
    name="trait",
    type_hint="str",
    required=False,
    description="For trait impls, the trait path being implemented (e.g. ``Display``).",
)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


def rust_kind_schema() -> KindSchema:
    """Return the :class:`KindSchema` exposed by the Rust backend.

    The vocabulary captures the top-level items agents need to address
    plus methods inside ``impl`` blocks (so they can be walked / removed).
    Struct fields, trait associated items, enum variants, and macro
    invocations are intentionally not addressable as children in v1 —
    mirroring the Go backend's "all top-level kinds are leaves" stance,
    with ``method`` as the single exception because Rust methods live
    nested inside ``impl`` blocks by syntax.
    """
    source_file = KindSpec(
        name="source_file",
        description="A Rust source file; the root of any parsed tree.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset({"use", "mod", "const", "static", "type", "fn", "struct", "enum", "trait", "impl"}),
    )
    use_item = KindSpec(
        name="use",
        description="A Rust ``use`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    mod_item = KindSpec(
        name="mod",
        description="A Rust ``mod`` declaration (file-level external or inline).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    const_item = KindSpec(
        name="const",
        description="A Rust ``const`` item.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    static_item = KindSpec(
        name="static",
        description="A Rust ``static`` item.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    type_item = KindSpec(
        name="type",
        description="A Rust ``type`` alias at module scope.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    fn_item = KindSpec(
        name="fn",
        description="A Rust top-level function declaration.",
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_GENERICS,
            _ATTR_WHERE,
            _ATTR_VISIBILITY,
            _ATTR_MODIFIERS,
        ),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    struct_item = KindSpec(
        name="struct",
        description="A Rust ``struct`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    enum_item = KindSpec(
        name="enum",
        description="A Rust ``enum`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    trait_item = KindSpec(
        name="trait",
        description="A Rust ``trait`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    impl_item = KindSpec(
        name="impl",
        description="A Rust ``impl`` block (inherent or trait impl).",
        attributes=(_ATTR_SELF_TYPE, _ATTR_TRAIT, _ATTR_BODY, _ATTR_GENERICS, _ATTR_WHERE),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    method_item = KindSpec(
        name="method",
        description=(
            "A Rust method inside an ``impl`` block. Walked and removable in v1; "
            "inserting a method into an existing impl requires rebuilding the impl "
            "because impl body ranges are not exposed."
        ),
        attributes=(
            _ATTR_NAME,
            _ATTR_PARAMETERS,
            _ATTR_RETURN,
            _ATTR_BODY,
            _ATTR_GENERICS,
            _ATTR_WHERE,
            _ATTR_VISIBILITY,
            _ATTR_MODIFIERS,
        ),
        # Empty allowed_parent_kinds: methods cannot be inserted anywhere
        # in v1 (impl body ranges are not exposed; source_file does not
        # accept raw methods). They exist in the kind vocabulary so
        # walk_symbols can return them with a named kind.
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )

    specs = (
        source_file,
        use_item,
        mod_item,
        const_item,
        static_item,
        type_item,
        fn_item,
        struct_item,
        enum_item,
        trait_item,
        impl_item,
        method_item,
    )
    return KindSchema(
        language_key="rust",
        source_kinds=frozenset({"source_file"}),
        kinds={s.name: s for s in specs},
    )


_RUST_KIND_SCHEMA = rust_kind_schema()


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


_RUST_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


class RustLogicalNameResolver:
    """Rust logical-name resolver.

    Logical names are dotted module paths (``foo.bar.baz``) that map to
    ``<project_root>/foo/bar/baz.rs``. Each segment must be a valid Rust
    identifier. This is a file-level resolver — intra-file navigation is
    :meth:`GoStructuralLanguage.walk_symbols`' job, not the resolver's.
    """

    def __init__(self, project_root: Path):
        self._project_root = project_root

    def parse(self, raw: str) -> LogicalName:
        if not raw:
            raise NameResolutionError(raw, "rust logical name must be non-empty")
        parts = tuple(raw.split("."))
        for segment in parts:
            if not _RUST_IDENTIFIER_RE.match(segment):
                raise NameResolutionError(
                    raw,
                    f"invalid identifier segment: {segment!r}",
                )
        return LogicalName(raw=raw, parts=parts)

    def resolve(self, name: LogicalName) -> NameResolution:
        if not name.parts:
            raise NameResolutionError(name.raw, "rust logical name must have at least one segment")
        *dirs, last = name.parts
        candidate = self._project_root.joinpath(*dirs, f"{last}.rs")
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
class _RustTree:
    """Opaque handle for a parsed Rust source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    """

    source: str


@dataclass(frozen=True)
class _RustSymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    :ivar kind: structural kind name.
    :ivar name_path: ``Parent/Child`` path within the source file (e.g.
        ``Point/norm`` for a method in ``impl Point``, or ``Point<Display>/fmt``
        for a method in ``impl Display for Point``).
    :ivar extent_offset: start byte of the symbol's declaration.
    :ivar extent_length: byte length of the symbol's declaration.
    :ivar body_range: always ``None`` for v1 — Rust body ranges are not
        exposed (no nested-symbol insertion into existing impls).
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _RustDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text, ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _RustPattern:
    """A compiled Rust pattern.

    :ivar source: pattern source text with ``$name`` / ``$_`` sigils.
    """

    source: str


# -----------------------------------------------------------------------------
# Subprocess bridge
# -----------------------------------------------------------------------------

_BRIDGE_PACKAGE_DIR = Path(__file__).resolve().parent / "rust_bridge"
_BRIDGE_BINARY_RELATIVE = Path("target") / "release" / "serena-rust-bridge"


def _bridge_binary_path() -> Path:
    return _BRIDGE_PACKAGE_DIR / _BRIDGE_BINARY_RELATIVE


def _build_bridge_if_needed() -> Path:
    """Compile the Rust bridge the first time the backend is used.

    Mirrors the Swift / Go bridges' lazy-build pattern: if the binary is
    absent, run ``cargo build --release`` under a file lock so concurrent
    first-use calls do not race. cargo handles dependency resolution
    (``syn``, ``proc-macro2``, ``serde_json``) via crates.io.
    """
    binary = _bridge_binary_path()
    if binary.exists():
        return binary
    _BRIDGE_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _BRIDGE_PACKAGE_DIR / ".build.lock"
    with lock_path.open("w") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        if binary.exists():
            return binary
        result = subprocess.run(
            ["cargo", "build", "--release"],
            check=False,
            cwd=str(_BRIDGE_PACKAGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ParseError(
                language_key="rust",
                source_preview="",
                detail=f"failed to build serena-rust-bridge:\n{result.stderr}",
            )
    if not binary.exists():
        raise ParseError(
            language_key="rust",
            source_preview="",
            detail=f"cargo build reported success but binary is missing at {binary}",
        )
    return binary


class _RustBridge:
    """Long-lived ``serena-rust-bridge`` subprocess.

    One bridge is shared by a single :class:`RustStructuralLanguage`
    instance. Calls are serialized by a mutex so concurrent Python callers
    do not interleave their JSON frames.
    """

    def __init__(self, binary_path: Path):
        self._binary_path = binary_path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
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
                    language_key="rust",
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


def _where_clause(where: str | None) -> str:
    if not where:
        return ""
    return f"\nwhere\n    {where}\n"


def _return_clause(return_type: str | None) -> str:
    if not return_type:
        return ""
    return f" -> {return_type}"


def _prefix_tokens(visibility: str | None, modifiers: str | None) -> str:
    parts: list[str] = []
    if visibility:
        parts.append(visibility)
    if modifiers:
        parts.append(modifiers)
    joined = " ".join(p for p in parts if p)
    return f"{joined} " if joined else ""


def _require_starts_with(statement: str, keyword: str, kind: KindName) -> None:
    """Require the first non-whitespace, non-visibility token to be ``keyword``.

    Visibility (``pub`` / ``pub(crate)`` / ``pub(super)`` / ``pub(in …)``)
    may precede the keyword.
    """
    stripped = statement.lstrip()
    if stripped.startswith("pub"):
        # skip past pub, pub(crate), pub(...)
        idx = 3
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
    head = stripped.split(None, 1)
    if not head or head[0] != keyword:
        raise DeclarationError(kind, f"{kind} statement must start with {keyword!r}")


# -----------------------------------------------------------------------------
# Pattern rendering (replacement templating)
# -----------------------------------------------------------------------------


_CAPTURE_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)")


def _render_replacement_template(replacement_source: str, bindings: Mapping[str, Any]) -> str:
    """Substitute ``$name`` references in ``replacement_source`` with
    ``bindings[name]`` converted to source text. Bindings may be raw strings
    or :class:`_RustDeclaration` handles.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise PatternError(reason="binding", detail=f"no binding for ${name}")
        value = bindings[name]
        if isinstance(value, _RustDeclaration):
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


class RustStructuralLanguage(StructuralLanguage):
    """Rust structural-language backend driven by ``syn`` via subprocess.

    One backend instance owns one long-lived bridge subprocess. Disposing
    the instance (``close()`` or GC) tears the subprocess down cleanly.
    """

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        binary_path: Path | None = None,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`RustLogicalNameResolver` rooted at CWD.
        :param binary_path: explicit path to the bridge binary. If ``None``,
            uses the bundled bridge built lazily on first use.
        """
        self._name_resolver = name_resolver or RustLogicalNameResolver(Path.cwd())
        self._explicit_binary = binary_path
        self._bridge: _RustBridge | None = None

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "rust"

    @property
    def kind_schema(self) -> KindSchema:
        return _RUST_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- bridge access -----------------------------------------------------

    def _get_bridge(self) -> _RustBridge:
        if self._bridge is None:
            binary = self._explicit_binary or _build_bridge_if_needed()
            self._bridge = _RustBridge(binary)
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

    def parse(self, source: str) -> _RustTree:
        # Parse is byte-identical by construction (we wrap source verbatim);
        # the bridge is only invoked on walk_symbols and mutation ops.
        return _RustTree(source=source)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _RustTree):
            return tree.source
        if isinstance(tree, _RustDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _RustTree):
            raise TypeError(f"root_kind expects a _RustTree; got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _RustTree):
            raise TypeError(f"walk_symbols expects a _RustTree; got {type(tree).__name__}")
        response = self._get_bridge().call("walk_symbols", source=tree.source)
        _raise_for_error(response)
        for entry in response.get("symbols", []):
            body = entry.get("body_range")
            ref = _RustSymbolRef(
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
    ) -> _RustDeclaration:
        self.kind_schema.get(kind)  # validate kind exists
        children_list = list(children)

        for child in children_list:
            if not isinstance(child, _RustDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _RustDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind in {"use", "mod", "const", "static", "type", "struct", "enum", "trait"}:
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, kind, kind)
            return _RustDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "fn":
            return self._build_fn_like(kind, attributes, keyword="fn")

        if kind == "method":
            return self._build_fn_like(kind, attributes, keyword="fn")

        if kind == "impl":
            return self._build_impl(kind, attributes)

        raise DeclarationError(kind, f"unsupported kind in Rust backend: {kind!r}")

    def _build_fn_like(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
        *,
        keyword: str,
    ) -> _RustDeclaration:
        name = _require_str(attributes, "name", kind)
        params = _optional_str(attributes, "parameters", kind)
        return_type = _optional_str_or_none(attributes, "return_type", kind)
        body = _optional_str(attributes, "body", kind)
        generics = _optional_str_or_none(attributes, "generics", kind)
        where_clause = _optional_str_or_none(attributes, "where_clause", kind)
        visibility = _optional_str_or_none(attributes, "visibility", kind)
        modifiers = _optional_str_or_none(attributes, "modifiers", kind)
        body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
        where_str = _where_clause(where_clause)
        rendered = (
            f"{_prefix_tokens(visibility, modifiers)}{keyword} {name}"
            f"{_generics_clause(generics)}({params})"
            f"{_return_clause(return_type)}"
            f"{where_str}"
            f" {{\n{body_inner}}}\n"
        )
        return _RustDeclaration(kind=kind, source=rendered)

    def _build_impl(self, kind: KindName, attributes: Mapping[str, Any]) -> _RustDeclaration:
        self_type = _require_str(attributes, "self_type", kind)
        trait_path = _optional_str_or_none(attributes, "trait", kind)
        generics = _optional_str_or_none(attributes, "generics", kind)
        where_clause = _optional_str_or_none(attributes, "where_clause", kind)
        body = _optional_str(attributes, "body", kind)
        body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
        where_str = _where_clause(where_clause)
        generic_clause = _generics_clause(generics)
        head = f"impl{generic_clause} "
        if trait_path:
            head += f"{trait_path} for {self_type}"
        else:
            head += self_type
        rendered = f"{head}{where_str} {{\n{body_inner}}}\n"
        return _RustDeclaration(kind=kind, source=rendered)

    # ---- mutation ----------------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _RustTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _RustDeclaration):
            raise TypeError(f"child must be a _RustDeclaration; got {type(child).__name__}")

        tree, parent_path = self._resolve_insertion_parent(parent)
        anchor_path = anchor.name_path if isinstance(anchor, _RustSymbolRef) else None
        response = self._get_bridge().call(
            "insert_child",
            source=tree.source,
            parent_name_path=parent_path,
            anchor_name_path=anchor_path,
            position=position,
            child_source=child.source,
        )
        _raise_for_error(response)
        return _RustTree(source=response["source"])

    def remove_child(self, parent: Any, child: Any) -> _RustTree:
        if not isinstance(child, _RustSymbolRef):
            raise TypeError(f"child must be a _RustSymbolRef; got {type(child).__name__}")
        tree, _ = self._resolve_insertion_parent(parent)
        response = self._get_bridge().call(
            "remove_child",
            source=tree.source,
            child_name_path=child.name_path,
        )
        _raise_for_error(response)
        return _RustTree(source=response["source"])

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_RustTree, str | None]:
        """Normalize ``parent`` to ``(tree, parent_name_path_or_None)``.

        Accepts only a whole tree — nested insertion via a bare
        :class:`_RustSymbolRef` is not supported in v1 (no body ranges are
        exposed), matching the Swift / Go backend contract. In particular,
        this means adding a method to an existing impl requires rebuilding
        the impl block; it cannot be done as a sub-tree mutation.
        """
        if isinstance(parent, _RustTree):
            return parent, None
        raise TypeError(
            f"parent must be a _RustTree; got {type(parent).__name__}. Pass the tree and use anchor / position to target a nested symbol."
        )

    # ---- pattern matching --------------------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError(reason="parse", detail="empty pattern")
        return _RustPattern(source=pattern_source)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _RustTree):
            raise TypeError(f"find_matches expects a _RustTree; got {type(tree).__name__}")
        if not isinstance(pattern, _RustPattern):
            raise TypeError(f"pattern must be a _RustPattern; got {type(pattern).__name__}")
        scope_path: str | None = None
        if isinstance(scope, _RustSymbolRef):
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
            node_ref = _RustSymbolRef(
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
    ) -> _RustDeclaration:
        if not replacement_source:
            raise PatternError(reason="parse", detail="empty replacement")
        rendered = _render_replacement_template(replacement_source, bindings)
        return _RustDeclaration(kind="_replacement", source=rendered)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _RustTree:
        if not isinstance(tree, _RustTree):
            raise TypeError(f"apply_replacement expects a _RustTree; got {type(tree).__name__}")
        if not isinstance(replacement, _RustDeclaration):
            raise TypeError(f"replacement must be a _RustDeclaration; got {type(replacement).__name__}")
        if not isinstance(match.node, _RustSymbolRef):
            raise TypeError(f"match.node must be a _RustSymbolRef; got {type(match.node).__name__}")
        response = self._get_bridge().call(
            "apply_replacement",
            source=tree.source,
            match_offset=match.node.extent_offset,
            match_length=match.node.extent_length,
            replacement_source=replacement.source,
        )
        _raise_for_error(response)
        return _RustTree(source=response["source"])

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _RustTree:
        if source_kind not in self.kind_schema.source_kinds:
            raise DeclarationError(
                source_kind,
                f"not a source kind in {self.language_key}; valid: {sorted(self.kind_schema.source_kinds)}",
            )
        return _RustTree(source="")


def _raise_for_error(response: Mapping[str, Any]) -> None:
    """Map a bridge error response onto the structural error hierarchy."""
    if response.get("ok", False):
        return
    kind = str(response.get("error_kind", "bridge"))
    message = str(response.get("message", ""))
    if kind == "pattern_parse":
        raise PatternError(reason="parse", detail=message)
    if kind in {"symbol_missing", "no_body", "bad_request"}:
        raise ValueError(f"rust bridge: {message}")
    if kind == "unknown_op":
        raise RuntimeError(f"rust bridge: {message}")
    raise ParseError(language_key="rust", source_preview="", detail=f"{kind}: {message}")
