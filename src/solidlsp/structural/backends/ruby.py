"""Ruby structural backend.

Bridges :class:`~solidlsp.structural.base.StructuralLanguage` to Ruby's
``Prism`` parser (stdlib since Ruby 3.3), which runs as a long-lived Ruby
subprocess (see :mod:`ruby_bridge`).

The handle shape mirrors the Swift / TypeScript / Go / Rust / Java backends:
parse wraps source verbatim, every mutation applies a text-level edit and
re-parses via the subprocess, and symbols are value-typed refs (kind +
name_path + byte offsets) that survive across re-parses. Byte offsets on
the wire are UTF-8 byte offsets, which is what Prism's ``Location``
``#start_offset`` and ``#end_offset`` already emit.

Ruby is a runtime-interpreter backend: no compile or install step is
needed. Prism ships as part of the Ruby standard library, so the bridge
is simply ``ruby bridge.rb`` with no Gemfile or vendor directory.
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
    description="The full Ruby statement text, rendered verbatim.",
)

_ATTR_PARAMETERS = AttributeSpec(
    name="parameters",
    type_hint="str",
    required=False,
    description="Parameter list interior, no parentheses.",
)

_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=False,
    description="Method body text placed between ``def`` / ``end``.",
)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


def ruby_kind_schema() -> KindSchema:
    """Return the :class:`KindSchema` exposed by the Ruby backend.

    Eight kinds: ``source_file`` as the root; ``class`` / ``module`` /
    ``method`` / ``constant`` / ``require`` / ``alias`` as top-level
    insertables; ``singleton_method`` (``def self.x``) is walked only
    (never insertable, because it is always a member of the enclosing
    class or module and v1 does not expose class / module body ranges).
    """
    source_file = KindSpec(
        name="source_file",
        description="A Ruby source file; the root of any parsed tree.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset({"class", "module", "method", "constant", "require", "alias"}),
    )
    class_item = KindSpec(
        name="class",
        description="A Ruby ``class`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    module_item = KindSpec(
        name="module",
        description="A Ruby ``module`` declaration.",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    method_item = KindSpec(
        name="method",
        description=(
            "A Ruby method (``def foo``). Insertable at the top level in v1; "
            "walked and removable when found inside a class / module, but "
            "inserting into an existing type requires rebuilding the type "
            "because body ranges are not exposed."
        ),
        attributes=(_ATTR_NAME, _ATTR_PARAMETERS, _ATTR_BODY),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    singleton_method_item = KindSpec(
        name="singleton_method",
        description=(
            "A Ruby singleton method (``def self.foo`` / ``def Class.foo``). "
            "Walked and removable when found inside a class / module; never "
            "insertable because it is always a member of the enclosing type."
        ),
        attributes=(_ATTR_NAME, _ATTR_PARAMETERS, _ATTR_BODY),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    constant_item = KindSpec(
        name="constant",
        description=(
            "A Ruby constant assignment (``CONST = value`` / "
            "``Foo::BAR = value``). Insertable at the top level; walked and "
            "removable when found inside a class / module."
        ),
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    require_item = KindSpec(
        name="require",
        description=(
            "A Ruby ``require`` / ``require_relative`` call with a literal "
            "string argument. Calls with non-literal arguments are not "
            "recognised as requires."
        ),
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )
    alias_item = KindSpec(
        name="alias",
        description="A Ruby ``alias`` statement (``alias new_name old_name``).",
        attributes=(_ATTR_STATEMENT,),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )

    specs = (
        source_file,
        class_item,
        module_item,
        method_item,
        singleton_method_item,
        constant_item,
        require_item,
        alias_item,
    )
    return KindSchema(
        language_key="ruby",
        source_kinds=frozenset({"source_file"}),
        kinds={s.name: s for s in specs},
    )


_RUBY_KIND_SCHEMA = ruby_kind_schema()


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


_RUBY_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


class RubyLogicalNameResolver:
    """Ruby logical-name resolver.

    Logical names are dotted snake_case segments (``foo.bar``) that map to
    ``<project_root>/foo/bar.rb``. Each segment must be a valid Ruby
    identifier (Ruby-lowercase constants / snake_case file stems). This is
    a file-level resolver -- intra-file navigation is
    :meth:`RubyStructuralLanguage.walk_symbols`' job, not the resolver's.
    """

    def __init__(self, project_root: Path):
        self._project_root = project_root

    def parse(self, raw: str) -> LogicalName:
        if not raw:
            raise NameResolutionError(raw, "ruby logical name must be non-empty")
        parts = tuple(raw.split("."))
        for segment in parts:
            if not _RUBY_IDENTIFIER_RE.match(segment):
                raise NameResolutionError(
                    raw,
                    f"invalid identifier segment: {segment!r}",
                )
        return LogicalName(raw=raw, parts=parts)

    def resolve(self, name: LogicalName) -> NameResolution:
        if not name.parts:
            raise NameResolutionError(name.raw, "ruby logical name must have at least one segment")
        *dirs, last = name.parts
        candidate = self._project_root.joinpath(*dirs, f"{last}.rb")
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
class _RubyTree:
    """Opaque handle for a parsed Ruby source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    """

    source: str


@dataclass(frozen=True)
class _RubySymbolRef:
    """A value-typed reference to a named symbol in a tree's source.

    :ivar kind: structural kind name.
    :ivar name_path: ``Parent/Child`` path within the source file.
    :ivar extent_offset: start byte of the symbol's declaration.
    :ivar extent_length: byte length of the symbol's declaration.
    :ivar body_range: always ``None`` in v1 -- Ruby body ranges are not
        exposed (no nested-symbol insertion into existing classes / modules).
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _RubyDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered source text, ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _RubyPattern:
    """A compiled Ruby pattern.

    :ivar source: pattern source text with ``$name`` / ``$_`` sigils.
    """

    source: str


# -----------------------------------------------------------------------------
# Subprocess bridge
# -----------------------------------------------------------------------------

_BRIDGE_PACKAGE_DIR = Path(__file__).resolve().parent / "ruby_bridge"
_BRIDGE_ENTRY_RELATIVE = Path("bridge.rb")


def _bridge_entry_path() -> Path:
    """Absolute path to the bridge entry script."""
    return _BRIDGE_PACKAGE_DIR / _BRIDGE_ENTRY_RELATIVE


def _ruby_binary() -> str:
    """Resolve the ``ruby`` binary path.

    Preference order: ``SERENA_RUBY`` env override, Homebrew keg-only
    location (``/opt/homebrew/opt/ruby/bin/ruby`` on Apple Silicon,
    ``/usr/local/opt/ruby/bin/ruby`` on Intel), then bare ``ruby`` on
    PATH. Apple's system Ruby (2.6.10) is too old -- it predates Prism's
    inclusion in stdlib -- so bare ``ruby`` is only a last-ditch fallback.
    """
    override = os.environ.get("SERENA_RUBY")
    if override:
        return override
    for prefix in (Path("/opt/homebrew/opt/ruby"), Path("/usr/local/opt/ruby")):
        candidate = prefix / "bin" / "ruby"
        if candidate.exists():
            return str(candidate)
    on_path = shutil.which("ruby")
    if on_path:
        return on_path
    return "ruby"


class _RubyBridge:
    """Long-lived ``ruby bridge.rb`` subprocess.

    One bridge is shared by a single :class:`RubyStructuralLanguage`
    instance. Calls are serialized by a mutex so concurrent Python callers
    do not interleave their JSON frames.
    """

    def __init__(self, entry_path: Path):
        """:param entry_path: absolute path to the bridge entry script."""
        self._entry_path = entry_path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                [_ruby_binary(), str(self._entry_path)],
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
                    language_key="ruby",
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


def _ensure_trailing_newline(src: str) -> str:
    return src if src[-1:] == "\n" else src + "\n"


def _require_starts_with(statement: str, keyword: str, kind: KindName) -> None:
    """Require the first non-whitespace token of ``statement`` to be ``keyword``."""
    stripped = statement.lstrip()
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
    or :class:`_RubyDeclaration` handles.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise PatternError(reason="binding", detail=f"no binding for ${name}")
        value = bindings[name]
        if isinstance(value, _RubyDeclaration):
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


class RubyStructuralLanguage(StructuralLanguage):
    """Ruby structural-language backend driven by ``Prism`` via subprocess.

    One backend instance owns one long-lived bridge subprocess. Disposing
    the instance (``close()`` or GC) tears the subprocess down cleanly.
    """

    def __init__(
        self,
        name_resolver: LogicalNameResolver | None = None,
        entry_path: Path | None = None,
    ):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
            Defaults to a :class:`RubyLogicalNameResolver` rooted at CWD.
        :param entry_path: explicit path to the bridge entry script. If
            ``None``, uses the bundled ``ruby_bridge/bridge.rb``.
        """
        self._name_resolver = name_resolver or RubyLogicalNameResolver(Path.cwd())
        self._explicit_entry = entry_path
        self._bridge: _RubyBridge | None = None

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "ruby"

    @property
    def kind_schema(self) -> KindSchema:
        return _RUBY_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- bridge access -----------------------------------------------------

    def _get_bridge(self) -> _RubyBridge:
        if self._bridge is None:
            entry = self._explicit_entry or _bridge_entry_path()
            if not entry.exists():
                raise ParseError(
                    language_key="ruby",
                    source_preview="",
                    detail=f"ruby bridge entry script missing at {entry}",
                )
            self._bridge = _RubyBridge(entry)
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

    def parse(self, source: str) -> _RubyTree:
        return _RubyTree(source=source)

    def serialize(self, tree: Any) -> str:
        if isinstance(tree, _RubyTree):
            return tree.source
        if isinstance(tree, _RubyDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _RubyTree):
            raise TypeError(f"root_kind expects a _RubyTree; got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _RubyTree):
            raise TypeError(f"walk_symbols expects a _RubyTree; got {type(tree).__name__}")
        response = self._get_bridge().call("walk_symbols", source=tree.source)
        _raise_for_error(response)
        for entry in response.get("symbols", []):
            body = entry.get("body_range")
            ref = _RubySymbolRef(
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
    ) -> _RubyDeclaration:
        self.kind_schema.get(kind)
        children_list = list(children)

        for child in children_list:
            if not isinstance(child, _RubyDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _RubyDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind == "class":
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, "class", kind)
            return _RubyDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "module":
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, "module", kind)
            return _RubyDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "require":
            statement = _require_str(attributes, "statement", kind)
            head = statement.lstrip().split(None, 1)
            if not head or head[0] not in ("require", "require_relative"):
                raise DeclarationError(kind, "require statement must start with 'require' or 'require_relative'")
            return _RubyDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "alias":
            statement = _require_str(attributes, "statement", kind)
            _require_starts_with(statement, "alias", kind)
            return _RubyDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "constant":
            statement = _require_str(attributes, "statement", kind)
            if "=" not in statement:
                raise DeclarationError(kind, "constant statement must contain '='")
            return _RubyDeclaration(kind=kind, source=_ensure_trailing_newline(statement))

        if kind == "method":
            return self._build_method(kind, attributes)

        if kind == "singleton_method":
            return self._build_singleton_method(kind, attributes)

        raise DeclarationError(kind, f"unsupported kind in Ruby backend: {kind!r}")

    def _build_method(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
    ) -> _RubyDeclaration:
        name = _require_str(attributes, "name", kind)
        if not _RUBY_METHOD_NAME_RE.match(name):
            raise DeclarationError(kind, f"invalid method name: {name!r}")
        params = _optional_str(attributes, "parameters", kind)
        body = _optional_str(attributes, "body", kind)
        header = f"def {name}({params})\n" if params else f"def {name}\n"
        body_text = body if (not body or body.endswith("\n")) else body + "\n"
        rendered = f"{header}{body_text}end\n"
        return _RubyDeclaration(kind=kind, source=rendered)

    def _build_singleton_method(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
    ) -> _RubyDeclaration:
        name = _require_str(attributes, "name", kind)
        if not name.startswith("self."):
            raise DeclarationError(
                kind,
                f"singleton_method name must start with 'self.': {name!r}",
            )
        tail = name[len("self.") :]
        if not _RUBY_METHOD_NAME_RE.match(tail):
            raise DeclarationError(kind, f"invalid singleton_method name: {name!r}")
        params = _optional_str(attributes, "parameters", kind)
        body = _optional_str(attributes, "body", kind)
        header = f"def {name}({params})\n" if params else f"def {name}\n"
        body_text = body if (not body or body.endswith("\n")) else body + "\n"
        rendered = f"{header}{body_text}end\n"
        return _RubyDeclaration(kind=kind, source=rendered)

    # ---- mutation ----------------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _RubyTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _RubyDeclaration):
            raise TypeError(f"child must be a _RubyDeclaration; got {type(child).__name__}")

        tree, parent_path = self._resolve_insertion_parent(parent)
        anchor_path = anchor.name_path if isinstance(anchor, _RubySymbolRef) else None
        response = self._get_bridge().call(
            "insert_child",
            source=tree.source,
            parent_name_path=parent_path,
            anchor_name_path=anchor_path,
            position=position,
            child_source=child.source,
        )
        _raise_for_error(response)
        return _RubyTree(source=response["source"])

    def remove_child(self, parent: Any, child: Any) -> _RubyTree:
        if not isinstance(child, _RubySymbolRef):
            raise TypeError(f"child must be a _RubySymbolRef; got {type(child).__name__}")
        tree, _ = self._resolve_insertion_parent(parent)
        response = self._get_bridge().call(
            "remove_child",
            source=tree.source,
            child_name_path=child.name_path,
        )
        _raise_for_error(response)
        return _RubyTree(source=response["source"])

    def _resolve_insertion_parent(self, parent: Any) -> tuple[_RubyTree, str | None]:
        """Normalize ``parent`` to ``(tree, parent_name_path_or_None)``.

        Accepts only a whole tree -- nested insertion via a bare
        :class:`_RubySymbolRef` is not supported in v1 (no body ranges are
        exposed), matching the Swift / Go / Rust / Java backend contract.
        In particular, adding a method to an existing class requires
        rebuilding the class block; it cannot be done as a sub-tree
        mutation.
        """
        if isinstance(parent, _RubyTree):
            return parent, None
        raise TypeError(
            f"parent must be a _RubyTree; got {type(parent).__name__}. Pass the tree and use anchor / position to target a nested symbol."
        )

    # ---- pattern matching --------------------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError(reason="parse", detail="empty pattern")
        return _RubyPattern(source=pattern_source)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _RubyTree):
            raise TypeError(f"find_matches expects a _RubyTree; got {type(tree).__name__}")
        if not isinstance(pattern, _RubyPattern):
            raise TypeError(f"pattern must be a _RubyPattern; got {type(pattern).__name__}")
        scope_path: str | None = None
        if isinstance(scope, _RubySymbolRef):
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
            node_ref = _RubySymbolRef(
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
    ) -> _RubyDeclaration:
        if not replacement_source:
            raise PatternError(reason="parse", detail="empty replacement")
        rendered = _render_replacement_template(replacement_source, bindings)
        return _RubyDeclaration(kind="_replacement", source=rendered)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _RubyTree:
        if not isinstance(tree, _RubyTree):
            raise TypeError(f"apply_replacement expects a _RubyTree; got {type(tree).__name__}")
        if not isinstance(replacement, _RubyDeclaration):
            raise TypeError(f"replacement must be a _RubyDeclaration; got {type(replacement).__name__}")
        if not isinstance(match.node, _RubySymbolRef):
            raise TypeError(f"match.node must be a _RubySymbolRef; got {type(match.node).__name__}")
        response = self._get_bridge().call(
            "apply_replacement",
            source=tree.source,
            match_offset=match.node.extent_offset,
            match_length=match.node.extent_length,
            replacement_source=replacement.source,
        )
        _raise_for_error(response)
        return _RubyTree(source=response["source"])

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _RubyTree:
        if source_kind not in self.kind_schema.source_kinds:
            raise DeclarationError(
                source_kind,
                f"not a source kind in {self.language_key}; valid: {sorted(self.kind_schema.source_kinds)}",
            )
        return _RubyTree(source="")


# Method names may be identifiers optionally followed by ``?``, ``!``, or
# ``=`` (predicate / bang / setter forms). Operator methods (``+``, ``[]``,
# ``<=>`` ...) are not accepted as structured-renderer inputs in v1;
# agents wanting those should use ``build_declaration("class", {"statement":
# "class Foo; def +(other); ...; end; end"})`` instead.
_RUBY_METHOD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*[?!=]?$")


def _raise_for_error(response: Mapping[str, Any]) -> None:
    """Map a bridge error response onto the structural error hierarchy."""
    if response.get("ok", False):
        return
    kind = str(response.get("error_kind", "bridge"))
    message = str(response.get("message", ""))
    if kind == "pattern_parse":
        raise PatternError(reason="parse", detail=message)
    if kind in {"symbol_missing", "no_body", "bad_request"}:
        raise ValueError(f"ruby bridge: {message}")
    if kind == "unknown_op":
        raise RuntimeError(f"ruby bridge: {message}")
    raise ParseError(language_key="ruby", source_preview="", detail=f"{kind}: {message}")
