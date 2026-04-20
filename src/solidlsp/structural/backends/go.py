"""Go structural backend.

Bridges :class:`~solidlsp.structural.base.StructuralLanguage` to Go's
``go/ast`` / ``go/parser`` libraries, which run as a long-lived subprocess
binary (see :mod:`go_bridge`).

The handle shape mirrors the Swift and TypeScript backends: parse wraps
source verbatim, every mutation applies a text-level edit and re-parses via
the subprocess, and symbols are value-typed refs (kind + name_path + byte
offsets) that survive across re-parses. Byte offsets on the wire are
UTF-8 byte offsets, matching Go's native token.FileSet convention.
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
    description="The full Go statement text, rendered verbatim.",
)

_ATTR_PARAMETERS = AttributeSpec(
    name="parameters",
    type_hint="str",
    required=False,
    description="Parameter list interior, no parentheses.",
)

_ATTR_RESULT = AttributeSpec(
    name="result",
    type_hint="str",
    required=False,
    description="Return-type clause, e.g. 'int' or '(int, error)'.",
)

_ATTR_TYPE_PARAMETERS = AttributeSpec(
    name="type_parameters",
    type_hint="str",
    required=False,
    description="Generic type-parameter clause without square brackets (e.g. 'T any').",
)

_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=False,
    description="Function / method body text placed inside the braces.",
)

_ATTR_RECEIVER = AttributeSpec(
    name="receiver",
    type_hint="str",
    required=True,
    description="Receiver clause interior, e.g. 'r *Point' or 'p Point'.",
)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


def go_kind_schema() -> KindSchema:
    """Return the :class:`KindSchema` exposed by the Go backend."""
    source_file = KindSpec(
        name="source_file",
        description="A Go source file; the root of any parsed tree.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset({"package", "import", "const", "variable", "type", "function", "method"}),
    )
    pkg = KindSpec(
        name="package",
        description="A Go ``package`` clause; exactly one per source file.",
        attributes=(_ATTR_NAME,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    imp = KindSpec(
        name="import",
        description="A Go ``import`` declaration (single or grouped).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    const = KindSpec(
        name="const",
        description="A Go ``const`` declaration (single or grouped).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    var = KindSpec(
        name="variable",
        description="A Go ``var`` declaration (single or grouped).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    type_ = KindSpec(
        name="type",
        description="A Go ``type`` declaration (single or grouped; struct/interface/alias).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    fn = KindSpec(
        name="function",
        description="A Go top-level function declaration (no receiver).",
        attributes=(_ATTR_NAME, _ATTR_PARAMETERS, _ATTR_RESULT, _ATTR_BODY, _ATTR_TYPE_PARAMETERS),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    method = KindSpec(
        name="method",
        description="A Go method declaration (FuncDecl with a receiver).",
        attributes=(_ATTR_NAME, _ATTR_RECEIVER, _ATTR_PARAMETERS, _ATTR_RESULT, _ATTR_BODY, _ATTR_TYPE_PARAMETERS),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )

    specs = (source_file, pkg, imp, const, var, type_, fn, method)
    return KindSchema(
        language_key="go",
        source_kinds=frozenset({"source_file"}),
        kinds={s.name: s for s in specs},
    )


_GO_KIND_SCHEMA = go_kind_schema()


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


_GO_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


class GoLogicalNameResolver:
    """Go logical-name resolver.

    Logical names are dotted module paths (``foo.bar.baz``) that map to
    ``<project_root>/foo/bar/baz.go``. Each segment must be a valid Go
    identifier.
    """

    def __init__(self, project_root: Path):
        self._project_root = project_root

    def parse(self, raw: str) -> LogicalName:
        if not raw:
            raise NameResolutionError(raw, "go logical name must be non-empty")
        parts = tuple(raw.split("."))
        for segment in parts:
            if not _GO_IDENTIFIER_RE.match(segment):
                raise NameResolutionError(
                    raw,
                    f"invalid identifier segment: {segment!r}",
                )
        return LogicalName(raw=raw, parts=parts)

    def resolve(self, name: LogicalName) -> NameResolution:
        if not name.parts:
            raise NameResolutionError(name.raw, "go logical name must have at least one segment")
        *dirs, last = name.parts
        candidate = self._project_root.joinpath(*dirs, f"{last}.go")
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
class _GoTree:
    """Opaque handle for a parsed Go source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    """

    source: str


@dataclass(frozen=True)
class _GoSymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    :ivar kind: structural kind name.
    :ivar name_path: ``Parent/Child`` path within the source file (e.g.
        ``Point/Norm`` for a method).
    :ivar extent_offset: start byte of the symbol's declaration.
    :ivar extent_length: byte length of the symbol's declaration.
    :ivar body_range: always ``None`` for v1 — Go body ranges are not
        exposed (no nested-symbol insertion).
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _GoDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text, ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _GoPattern:
    """A compiled Go pattern.

    :ivar source: pattern source text with ``$name`` / ``$_`` sigils.
    """

    source: str


# -----------------------------------------------------------------------------
# Subprocess bridge
# -----------------------------------------------------------------------------

_BRIDGE_PACKAGE_DIR = Path(__file__).resolve().parent / "go_bridge"
_BRIDGE_BINARY_RELATIVE = Path("serena-go-bridge")


def _bridge_binary_path() -> Path:
    return _BRIDGE_PACKAGE_DIR / _BRIDGE_BINARY_RELATIVE


def _build_bridge_if_needed() -> Path:
    """Compile the Go bridge the first time the backend is used.

    Mirrors the Swift bridge's lazy-build pattern: if the binary is absent,
    run ``go build`` under a file lock so concurrent first-use calls do
    not race. Go's stdlib-only source means no dependencies need to be
    downloaded.
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
            ["go", "build", "-o", str(binary), "."],
            check=False,
            cwd=str(_BRIDGE_PACKAGE_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ParseError(
                language_key="go",
                source_preview="",
                detail=f"failed to build serena-go-bridge:\n{result.stderr}",
            )
    if not binary.exists():
        raise ParseError(
            language_key="go",
            source_preview="",
            detail=f"go build reported success but binary is missing at {binary}",
        )
    return binary


class _GoBridge:
    """Long-lived ``serena-go-bridge`` subprocess.

    One bridge is shared by a single :class:`GoStructuralLanguage`
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
                    language_key="go",
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


def _generic_clause(generic: str | None) -> str:
    return f"[{generic}]" if generic else ""


def _result_clause(result: str | None) -> str:
    if not result:
        return ""
    # leading space; callers do not add their own
    return f" {result}"


# -----------------------------------------------------------------------------
# Pattern rendering (replacement templating)
# -----------------------------------------------------------------------------


_CAPTURE_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)")


def _render_replacement_template(replacement_source: str, bindings: Mapping[str, Any]) -> str:
    """Substitute ``$name`` references in ``replacement_source`` with
    ``bindings[name]`` converted to source text. Bindings may be raw strings
    or :class:`_GoDeclaration` handles.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise PatternError(reason="binding", detail=f"no binding for ${name}")
        value = bindings[name]
        if isinstance(value, _GoDeclaration):
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


class GoStructuralLanguage(StructuralLanguage):
    """Go structural-language backend driven by ``go/ast`` via subprocess.

    One backend instance owns one long-lived bridge subprocess. Disposing
    the instance (``close()`` or GC) tears the subprocess down cleanly.
    """

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        binary_path: Path | None = None,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`GoLogicalNameResolver` rooted at CWD.
        :param binary_path: explicit path to the bridge binary. If ``None``,
            uses the bundled bridge built lazily on first use.
        """
        self._name_resolver = name_resolver or GoLogicalNameResolver(Path.cwd())
        self._explicit_binary = binary_path
        self._bridge: _GoBridge | None = None

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "go"

    @property
    def kind_schema(self) -> KindSchema:
        return _GO_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- bridge access -----------------------------------------------------

    def _get_bridge(self) -> _GoBridge:
        if self._bridge is None:
            binary = self._explicit_binary or _build_bridge_if_needed()
            self._bridge = _GoBridge(binary)
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

    def parse(self, source: str) -> _GoTree:
        # Parse is byte-identical by construction (we wrap source verbatim);
        # the bridge is only invoked on walk_symbols and mutation ops.
        return _GoTree(source=source)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _GoTree):
            return tree.source
        if isinstance(tree, _GoDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _GoTree):
            raise TypeError(f"root_kind expects a _GoTree; got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _GoTree):
            raise TypeError(f"walk_symbols expects a _GoTree; got {type(tree).__name__}")
        response = self._get_bridge().call("walk_symbols", source=tree.source)
        _raise_for_error(response)
        for entry in response.get("symbols", []):
            body = entry.get("body_range")
            ref = _GoSymbolRef(
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
    ) -> _GoDeclaration:
        self.kind_schema.get(kind)  # validate kind exists
        children_list = list(children)

        for child in children_list:
            if not isinstance(child, _GoDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _GoDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind == "package":
            name = _require_str(attributes, "name", kind)
            return _GoDeclaration(kind=kind, source=_ensure_trailing_newline(f"package {name}"))

        if kind in {"import", "const", "variable", "type"}:
            statement = _require_str(attributes, "statement", kind)
            stripped = statement.lstrip()
            keyword = {"import": "import", "const": "const", "variable": "var", "type": "type"}[kind]
            head = stripped.split(None, 1)
            if not head or head[0] != keyword:
                raise DeclarationError(kind, f"{kind} statement must start with {keyword!r}")
            return _GoDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "function":
            name = _require_str(attributes, "name", kind)
            params = _optional_str(attributes, "parameters", kind)
            result = _optional_str_or_none(attributes, "result", kind)
            body = _optional_str(attributes, "body", kind)
            generic = _optional_str_or_none(attributes, "type_parameters", kind)
            body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
            rendered = f"func {name}{_generic_clause(generic)}({params}){_result_clause(result)} {{\n{body_inner}}}\n"
            return _GoDeclaration(kind=kind, source=rendered)

        if kind == "method":
            name = _require_str(attributes, "name", kind)
            receiver = _require_str(attributes, "receiver", kind)
            params = _optional_str(attributes, "parameters", kind)
            result = _optional_str_or_none(attributes, "result", kind)
            body = _optional_str(attributes, "body", kind)
            generic = _optional_str_or_none(attributes, "type_parameters", kind)
            body_inner = body if (not body or body[-1:] == "\n") else body + "\n"
            rendered = f"func ({receiver}) {name}{_generic_clause(generic)}({params}){_result_clause(result)} {{\n{body_inner}}}\n"
            return _GoDeclaration(kind=kind, source=rendered)

        raise DeclarationError(kind, f"unsupported kind in Go backend: {kind!r}")

    # ---- mutation ----------------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _GoTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _GoDeclaration):
            raise TypeError(f"child must be a _GoDeclaration; got {type(child).__name__}")

        tree, parent_path = self._resolve_insertion_parent(parent)
        anchor_path = anchor.name_path if isinstance(anchor, _GoSymbolRef) else None
        response = self._get_bridge().call(
            "insert_child",
            source=tree.source,
            parent_name_path=parent_path,
            anchor_name_path=anchor_path,
            position=position,
            child_source=child.source,
        )
        _raise_for_error(response)
        return _GoTree(source=response["source"])

    def remove_child(self, parent: Any, child: Any) -> _GoTree:
        if not isinstance(child, _GoSymbolRef):
            raise TypeError(f"child must be a _GoSymbolRef; got {type(child).__name__}")
        tree, _ = self._resolve_insertion_parent(parent)
        response = self._get_bridge().call(
            "remove_child",
            source=tree.source,
            child_name_path=child.name_path,
        )
        _raise_for_error(response)
        return _GoTree(source=response["source"])

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_GoTree, str | None]:
        """Normalize ``parent`` to ``(tree, parent_name_path_or_None)``.

        Accepts only a whole tree — nested insertion via a bare
        :class:`_GoSymbolRef` is not supported in v1 (no body ranges are
        exposed), matching the Swift backend's contract.
        """
        if isinstance(parent, _GoTree):
            return parent, None
        raise TypeError(
            f"parent must be a _GoTree; got {type(parent).__name__}. Pass the tree and use anchor / position to target a nested symbol."
        )

    # ---- pattern matching --------------------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError(reason="parse", detail="empty pattern")
        return _GoPattern(source=pattern_source)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _GoTree):
            raise TypeError(f"find_matches expects a _GoTree; got {type(tree).__name__}")
        if not isinstance(pattern, _GoPattern):
            raise TypeError(f"pattern must be a _GoPattern; got {type(pattern).__name__}")
        scope_path: str | None = None
        if isinstance(scope, _GoSymbolRef):
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
            node_ref = _GoSymbolRef(
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
    ) -> _GoDeclaration:
        if not replacement_source:
            raise PatternError(reason="parse", detail="empty replacement")
        rendered = _render_replacement_template(replacement_source, bindings)
        return _GoDeclaration(kind="_replacement", source=rendered)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _GoTree:
        if not isinstance(tree, _GoTree):
            raise TypeError(f"apply_replacement expects a _GoTree; got {type(tree).__name__}")
        if not isinstance(replacement, _GoDeclaration):
            raise TypeError(f"replacement must be a _GoDeclaration; got {type(replacement).__name__}")
        if not isinstance(match.node, _GoSymbolRef):
            raise TypeError(f"match.node must be a _GoSymbolRef; got {type(match.node).__name__}")
        response = self._get_bridge().call(
            "apply_replacement",
            source=tree.source,
            match_offset=match.node.extent_offset,
            match_length=match.node.extent_length,
            replacement_source=replacement.source,
        )
        _raise_for_error(response)
        return _GoTree(source=response["source"])

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _GoTree:
        if source_kind not in self.kind_schema.source_kinds:
            raise DeclarationError(
                source_kind,
                f"not a source kind in {self.language_key}; valid: {sorted(self.kind_schema.source_kinds)}",
            )
        return _GoTree(source="")


def _raise_for_error(response: Mapping[str, Any]) -> None:
    """Map a bridge error response onto the structural error hierarchy."""
    if response.get("ok", False):
        return
    kind = str(response.get("error_kind", "bridge"))
    message = str(response.get("message", ""))
    if kind == "pattern_parse":
        raise PatternError(reason="parse", detail=message)
    if kind in {"symbol_missing", "no_body", "bad_request"}:
        raise ValueError(f"go bridge: {message}")
    if kind == "unknown_op":
        raise RuntimeError(f"go bridge: {message}")
    raise ParseError(language_key="go", source_preview="", detail=f"{kind}: {message}")
