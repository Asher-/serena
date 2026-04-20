"""Markdown :class:`StructuralLanguage` backend using markdown-it-py.

Follows the same offset-anchored source-edit shape as the C++ backend: the
opaque tree handle carries the original source verbatim, so
``serialize(parse(src)) == src`` is the zero-edit guarantee. Mutations compute
byte offsets from the markdown-it-py token stream's line maps, splice text
into the source, and re-parse.

Public entry points:

* :class:`MarkdownStructuralLanguage` — the :class:`StructuralLanguage` impl.
* :class:`MarkdownLogicalNameResolver` — slash/dotted path → ``.md`` file.
* :func:`markdown_kind_schema` — factory for the markdown kind vocabulary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.front_matter import front_matter_plugin

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

_ATTR_TEXT = AttributeSpec(
    name="text",
    type_hint="str",
    required=True,
    description="the rendered text of the heading as a single line (no trailing newline)",
)
_ATTR_LEVEL = AttributeSpec(
    name="level",
    type_hint="int",
    required=True,
    description="heading depth, 1 through 6 for ATX headings",
)
_ATTR_CONTENT = AttributeSpec(
    name="content",
    type_hint="str",
    required=True,
    description="raw source text of the block (preserved verbatim on insertion)",
)
_ATTR_LANGUAGE = AttributeSpec(
    name="language",
    type_hint="str | None",
    required=False,
    description="info-string for fenced code blocks (e.g. 'python'); None for an indented code block",
)
_ATTR_ORDERED = AttributeSpec(
    name="ordered",
    type_hint="bool",
    required=True,
    description="True for an ordered list (1. 2. 3.), False for a bullet list (- - -)",
)
_ATTR_BODY = AttributeSpec(
    name="body",
    type_hint="str",
    required=True,
    description="raw body content of a list item (markdown inline plus any nested blocks)",
)
_ATTR_LABEL = AttributeSpec(
    name="label",
    type_hint="str",
    required=True,
    description="reference label without the surrounding brackets",
)
_ATTR_DESTINATION = AttributeSpec(
    name="destination",
    type_hint="str",
    required=True,
    description="URL, relative path, or fragment the reference points at",
)
_ATTR_TITLE = AttributeSpec(
    name="title",
    type_hint="str | None",
    required=False,
    description="optional title string (quoted in the rendered source)",
)


def markdown_kind_schema() -> KindSchema:
    """Return the markdown structural kind vocabulary.

    :return: the kind schema exposed by :class:`MarkdownStructuralLanguage`.
    """
    # block: source-root
    source_file = KindSpec(
        name="source_file",
        description="A markdown document's top level.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(
            {
                "heading",
                "paragraph",
                "list",
                "code_block",
                "quote_block",
                "hr",
                "link_ref",
                "table",
                "html_block",
            }
        ),
    )

    # block: heading; kind-schema records it as a block under source_file,
    # even though walk_symbols presents headings hierarchically by level
    heading = KindSpec(
        name="heading",
        description="An ATX ('# ...') or setext ('===' / '---') heading.",
        attributes=(_ATTR_TEXT, _ATTR_LEVEL),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )

    # block: paragraph of inline content
    paragraph = KindSpec(
        name="paragraph",
        description="A paragraph of inline content separated by blank lines.",
        attributes=(_ATTR_CONTENT,),
        allowed_parent_kinds=frozenset({"source_file", "quote_block", "list_item"}),
        allowed_child_kinds=frozenset(),
    )

    # block: list container holding list_item children
    list_kind = KindSpec(
        name="list",
        description="An ordered or bullet list container.",
        attributes=(_ATTR_ORDERED,),
        allowed_parent_kinds=frozenset({"source_file", "quote_block", "list_item"}),
        allowed_child_kinds=frozenset({"list_item"}),
    )
    list_item = KindSpec(
        name="list_item",
        description="One entry within a list.",
        attributes=(_ATTR_BODY,),
        allowed_parent_kinds=frozenset({"list"}),
        allowed_child_kinds=frozenset(),
    )

    # block: code; either fenced (``` / ~~~) or indented
    code_block = KindSpec(
        name="code_block",
        description="A fenced or indented code block.",
        attributes=(_ATTR_CONTENT, _ATTR_LANGUAGE),
        allowed_parent_kinds=frozenset({"source_file", "quote_block", "list_item"}),
        allowed_child_kinds=frozenset(),
    )

    # block: quote / callout container; body is rendered verbatim with '> ' prefixes
    quote_block = KindSpec(
        name="quote_block",
        description="A blockquote container.",
        attributes=(_ATTR_CONTENT,),
        allowed_parent_kinds=frozenset({"source_file", "quote_block", "list_item"}),
        allowed_child_kinds=frozenset(),
    )

    # block: horizontal rule
    hr = KindSpec(
        name="hr",
        description="A horizontal rule (thematic break).",
        attributes=(),
        allowed_parent_kinds=frozenset({"source_file", "quote_block", "list_item"}),
        allowed_child_kinds=frozenset(),
    )

    # block: link reference definition
    link_ref = KindSpec(
        name="link_ref",
        description="A link reference definition ('[label]: destination').",
        attributes=(_ATTR_LABEL, _ATTR_DESTINATION, _ATTR_TITLE),
        allowed_parent_kinds=frozenset({"source_file"}),
        allowed_child_kinds=frozenset(),
    )

    # block: GFM-style table
    table = KindSpec(
        name="table",
        description="A pipe-delimited table; content is preserved verbatim.",
        attributes=(_ATTR_CONTENT,),
        allowed_parent_kinds=frozenset({"source_file", "quote_block", "list_item"}),
        allowed_child_kinds=frozenset(),
    )

    # block: raw HTML block
    html_block = KindSpec(
        name="html_block",
        description="A raw HTML block (preserved verbatim).",
        attributes=(_ATTR_CONTENT,),
        allowed_parent_kinds=frozenset({"source_file", "quote_block", "list_item"}),
        allowed_child_kinds=frozenset(),
    )

    # inline: link and image, exposed for pattern matching only (no build_declaration)
    link = KindSpec(
        name="link",
        description="An inline link ('[text](dest)').",
        attributes=(_ATTR_TEXT, _ATTR_DESTINATION, _ATTR_TITLE),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )
    image = KindSpec(
        name="image",
        description="An inline image ('![alt](dest)').",
        attributes=(_ATTR_TEXT, _ATTR_DESTINATION, _ATTR_TITLE),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=frozenset(),
    )

    return KindSchema(
        language_key="markdown",
        source_kinds=frozenset({"source_file"}),
        kinds={
            "source_file": source_file,
            "heading": heading,
            "paragraph": paragraph,
            "list": list_kind,
            "list_item": list_item,
            "code_block": code_block,
            "quote_block": quote_block,
            "hr": hr,
            "link_ref": link_ref,
            "table": table,
            "html_block": html_block,
            "link": link,
            "image": image,
        },
    )


_MARKDOWN_KIND_SCHEMA = markdown_kind_schema()


# =============================================================================
# Name resolver
# =============================================================================


class MarkdownLogicalNameResolver(LogicalNameResolver):
    """Maps a slashed or dotted logical name to a ``.md`` file under a root.

    :ivar _project_root: project root; resolutions report paths relative to it.
    :ivar _source_roots: ordered content directories to probe.
    :ivar _extensions: file extensions to try, in order.
    """

    _DEFAULT_EXTENSIONS: tuple[str, ...] = (".md", ".markdown")

    def __init__(
        self,
        project_root: Path,
        source_roots: Sequence[Path] = (),
        extensions: Sequence[str] = (),
    ):
        """:param project_root: directory at whose root paths are reported.
        :param source_roots: directories under which markdown files live;
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
        # grammar: slash- or dot-separated path; each part is a non-empty filename token
        if not raw:
            raise NameResolutionError(raw, "empty logical name")
        parts = tuple(re.split(r"[./]", raw))
        for part in parts:
            if not part or not _MARKDOWN_NAME_PART.fullmatch(part):
                raise NameResolutionError(raw, f"invalid markdown name part: {part!r}")
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
        return NameResolution(relative_path=str(relative), source_kind="source_file", exists=exists)


_MARKDOWN_NAME_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*")


# =============================================================================
# Opaque handles
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
class _MdTree:
    """Opaque handle for a parsed markdown source.

    :ivar source: original source text. Round-trip contract: ``serialize``
        returns this byte-for-byte when no edits have been applied.
    :ivar tokens: flat markdown-it-py token stream.
    :ivar line_starts: cumulative byte offsets of each line start, indexed by
        0-based line number; length is ``line_count + 1`` so the last entry is
        ``len(source)``.
    """

    source: str
    tokens: tuple[Token, ...]
    line_starts: tuple[int, ...]


@dataclass(frozen=True)
class _MdSymbolRef:
    """A value-typed reference to a structural node in a tree's source.

    :ivar kind: structural kind name.
    :ivar name_path: Parent/Child path within the source document.
    :ivar extent_offset: start byte of the node's declaration.
    :ivar extent_length: byte length of the node's declaration.
    :ivar body_range: for container nodes (heading scope, quote_block), the
        byte range of the body exclusive of its opening syntax. ``None`` for
        leaf nodes with no body.
    """

    kind: KindName
    name_path: str
    extent_offset: int
    extent_length: int
    body_range: tuple[int, int] | None


@dataclass(frozen=True)
class _MdDeclaration:
    """An opaque declaration built by :meth:`build_declaration`.

    :ivar kind: the declaration's structural kind.
    :ivar source: rendered markdown source text ready for insertion.
    """

    kind: KindName
    source: str


@dataclass(frozen=True)
class _MdPattern(AstPattern):  # type: ignore[misc]
    """A compiled pattern: the source form plus placeholder metadata.

    :ivar source: original pattern string (pre-encoding).
    :ivar pattern_tree: markdown-it-py :class:`SyntaxTreeNode` for the pattern's
        root block.
    :ivar placeholders: encoded-identifier → :class:`_Placeholder` lookup.
    """

    source: str
    pattern_tree: SyntaxTreeNode
    placeholders: Mapping[str, _Placeholder]


@dataclass(frozen=True)
class _Placeholder:
    """Metadata for one ``$``-sigil captured inside a pattern.

    :ivar name: the capture name the agent wrote; ``"_"`` is a wildcard.
    :ivar is_sequence: ``True`` iff the sigil was ``$*name``.
    :ivar encoded: synthetic identifier substituted into the pattern source.
    """

    name: str
    is_sequence: bool
    encoded: str


# =============================================================================
# The backend
# =============================================================================


_SIGIL_RE = re.compile(r"\$(?P<seq>\*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*|_)")
_SENTINEL_PREFIX = "SERENAPAT"
_SENTINEL_RE = re.compile(rf"{_SENTINEL_PREFIX}(?P<idx>\d+)")


class MarkdownStructuralLanguage(StructuralLanguage):
    """Structural backend for CommonMark / markdown sources.

    Backend design: parse with markdown-it-py, hold the source text verbatim on
    the tree handle, and serialize as the stored source. All mutations are
    byte-offset source edits computed from token line maps, followed by a
    re-parse. This mirrors the C++ backend's offset-rewrite model — the key
    difference being that markdown's grammar lives entirely in
    markdown-it-py's tokenizer, not a separate syntax library.
    """

    def __init__(self, name_resolver: LogicalNameResolver | None = None):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
        Defaults to a :class:`MarkdownLogicalNameResolver` rooted at the
        current working directory.
        """
        # markdown-it-py instances are cheap; one per backend is fine
        self._md = self._build_parser()
        self._name_resolver = name_resolver or MarkdownLogicalNameResolver(Path.cwd())

    @staticmethod
    def _build_parser() -> MarkdownIt:
        # commonmark core plus the table/front-matter plugins used in the wild
        parser = MarkdownIt("commonmark").enable("table").use(front_matter_plugin)
        return parser

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "markdown"

    @property
    def kind_schema(self) -> KindSchema:
        return _MARKDOWN_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- parse / serialize -------------------------------------------------

    def parse(self, source: str) -> _MdTree:
        # markdown-it-py is extremely forgiving; a ParseError is essentially
        # unreachable for non-adversarial inputs. Guard the call anyway so
        # failures surface with a source preview.
        try:
            tokens = tuple(self._md.parse(source))
        except Exception as err:  # pragma: no cover - defensive
            raise ParseError(language_key="markdown", source_preview=source[:240], detail=str(err)) from err
        line_starts = _compute_line_starts(source)
        return _MdTree(source=source, tokens=tokens, line_starts=line_starts)

    def serialize(self, tree: Any) -> str:
        # handles carry their source verbatim; no edits are cached on the handle
        if isinstance(tree, _MdTree):
            return tree.source
        if isinstance(tree, _MdDeclaration):
            return tree.source
        raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _MdTree):
            raise TypeError(f"root_kind expects a _MdTree, got {type(tree).__name__}")
        return "source_file"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        if not isinstance(tree, _MdTree):
            raise TypeError(f"walk_symbols expects a _MdTree, got {type(tree).__name__}")
        return list(_walk_headings(tree))

    # ---- declaration -------------------------------------------------------

    def build_declaration(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
        children: Iterable[Any],
    ) -> _MdDeclaration:
        self.kind_schema.get(kind)  # validates the kind name exists
        children_list = list(children)
        for child in children_list:
            if not isinstance(child, _MdDeclaration):
                raise DeclarationError(
                    kind,
                    f"child must be a _MdDeclaration from this backend; got {type(child).__name__}",
                )

        if kind == "source_file":
            raise DeclarationError(kind, "construct source_file via empty_source() plus insert_child()")

        if kind == "heading":
            text = _require_str(attributes, "text", kind)
            level = _require_int(attributes, "level", kind)
            if level < 1 or level > 6:
                raise DeclarationError(kind, f"heading level must be 1..6, got {level}")
            if "\n" in text:
                raise DeclarationError(kind, "heading text must not contain newlines")
            rendered = f"{'#' * level} {text}\n"
            return _MdDeclaration(kind=kind, source=rendered)

        if kind == "paragraph":
            content = _require_str(attributes, "content", kind)
            rendered = _ensure_single_trailing_newline(content.rstrip("\n"))
            return _MdDeclaration(kind=kind, source=rendered)

        if kind == "list":
            ordered = _require_bool(attributes, "ordered", kind)
            if not children_list:
                raise DeclarationError(kind, "list must contain at least one list_item child")
            for idx, child in enumerate(children_list):
                if child.kind != "list_item":
                    raise DeclarationError(kind, f"child {idx} must be a list_item, got {child.kind!r}")
            parts: list[str] = []
            for idx, child in enumerate(children_list):
                marker = f"{idx + 1}. " if ordered else "- "
                parts.append(_apply_list_marker(marker, child.source))
            joined = "".join(parts)
            return _MdDeclaration(kind=kind, source=_ensure_single_trailing_newline(joined.rstrip("\n")))

        if kind == "list_item":
            body = _require_str(attributes, "body", kind)
            # keep the raw body; the parent list prepends the marker and indents continuation lines
            return _MdDeclaration(kind=kind, source=body.rstrip("\n") + "\n")

        if kind == "code_block":
            content = _require_str(attributes, "content", kind)
            language = _optional_str_or_none(attributes, "language", kind)
            if language is None:
                # indented form: every non-empty line prefixed with four spaces
                indented = "\n".join(("    " + line if line else line) for line in content.splitlines())
                return _MdDeclaration(kind=kind, source=_ensure_single_trailing_newline(indented))
            if "\n" in language:
                raise DeclarationError(kind, "code_block language must not contain newlines")
            body = content if content.endswith("\n") else content + "\n"
            rendered = f"```{language}\n{body}```\n"
            return _MdDeclaration(kind=kind, source=rendered)

        if kind == "quote_block":
            content = _require_str(attributes, "content", kind)
            quoted = "\n".join(_quote_prefix(line) for line in content.splitlines())
            rendered = _ensure_single_trailing_newline(quoted)
            return _MdDeclaration(kind=kind, source=rendered)

        if kind == "hr":
            return _MdDeclaration(kind=kind, source="---\n")

        if kind == "link_ref":
            label = _require_str(attributes, "label", kind)
            destination = _require_str(attributes, "destination", kind)
            title = _optional_str_or_none(attributes, "title", kind)
            if "\n" in label or "\n" in destination:
                raise DeclarationError(kind, "link_ref label and destination must not contain newlines")
            title_clause = f' "{title}"' if title is not None else ""
            rendered = f"[{label}]: {destination}{title_clause}\n"
            return _MdDeclaration(kind=kind, source=rendered)

        if kind == "table":
            content = _require_str(attributes, "content", kind)
            return _MdDeclaration(kind=kind, source=_ensure_single_trailing_newline(content.rstrip("\n")))

        if kind == "html_block":
            content = _require_str(attributes, "content", kind)
            return _MdDeclaration(kind=kind, source=_ensure_single_trailing_newline(content.rstrip("\n")))

        raise DeclarationError(kind, f"build_declaration is not supported for kind {kind!r} in the markdown backend")

    # ---- insert / remove ---------------------------------------------------

    def insert_child(
        self, parent: Any, child: Any, anchor: Any | None = None, position: str = "end"
    ) -> _MdTree:
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")
        if not isinstance(child, _MdDeclaration):
            raise TypeError(f"child must be a _MdDeclaration; got {type(child).__name__}")

        tree = self._resolve_tree(parent)
        offset = self._compute_insertion_offset(tree, anchor, position)
        edit_text = _ensure_single_trailing_newline(child.source.rstrip("\n"))
        # preserve block separation on both sides of the insertion
        prefix, suffix = _block_separators(tree.source, offset)
        edit = _Edit(offset=offset, length=0, replacement=prefix + edit_text + suffix)
        new_source = _apply_edits(tree.source, [edit])
        return self.parse(new_source)

    def remove_child(self, parent: Any, child: Any) -> _MdTree:
        tree = self._resolve_tree(parent)
        if not isinstance(child, _MdSymbolRef):
            raise TypeError(f"child to remove must be a _MdSymbolRef; got {type(child).__name__}")
        edit = _Edit(offset=child.extent_offset, length=child.extent_length, replacement="")
        new_source = _apply_edits(tree.source, [edit])
        return self.parse(new_source)

    def _resolve_tree(self, parent: Any) -> _MdTree:
        if isinstance(parent, _MdTree):
            return parent
        raise TypeError(f"parent must be _MdTree; got {type(parent).__name__}")

    def _compute_insertion_offset(
        self,
        tree: _MdTree,
        anchor: Any,
        position: str,
    ) -> int:
        if anchor is not None and not isinstance(anchor, _MdSymbolRef):
            raise TypeError(f"anchor must be _MdSymbolRef or None; got {type(anchor).__name__}")

        if anchor is None:
            # document-level start/end when no anchor
            if position == "start":
                return 0
            if position == "end":
                return len(tree.source)
            raise ValueError(f"position {position!r} requires an anchor")

        # anchor-relative positions
        if position == "start":
            if anchor.body_range is None:
                raise ValueError(f"anchor {anchor.name_path!r} has no body scope to insert into")
            return anchor.body_range[0]
        if position == "end":
            if anchor.body_range is None:
                raise ValueError(f"anchor {anchor.name_path!r} has no body scope to insert into")
            return anchor.body_range[1]
        if position == "before":
            return anchor.extent_offset
        # after
        return anchor.extent_offset + anchor.extent_length

    # ---- pattern matching & rewriting --------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError("parse", "pattern source is empty")
        encoded, placeholders = _encode_sigils(pattern_source)
        try:
            tokens = self._md.parse(encoded)
        except Exception as err:
            raise PatternError("parse", f"markdown-it rejected pattern source: {err}") from err
        if not tokens:
            raise PatternError("parse", "pattern source produced no tokens")
        tree = SyntaxTreeNode(list(tokens))
        # pick the first block-level child as the pattern root; a bare pattern
        # is almost always a single block
        children = list(tree.children)
        if not children:
            raise PatternError("parse", "pattern source produced no block-level nodes")
        return _MdPattern(source=pattern_source, pattern_tree=children[0], placeholders=placeholders)

    def find_matches(
        self, tree: Any, pattern: AstPattern, scope: Any | None = None
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _MdTree):
            raise TypeError(f"tree must be a _MdTree; got {type(tree).__name__}")
        if not isinstance(pattern, _MdPattern):
            raise TypeError(f"pattern must come from this backend's compile_pattern; got {type(pattern).__name__}")
        scope_ref = scope if isinstance(scope, _MdSymbolRef) else None
        return list(self._iter_matches(tree, scope_ref, pattern))

    def _iter_matches(
        self, tree: _MdTree, scope: _MdSymbolRef | None, pattern: _MdPattern
    ) -> Iterator[PatternMatch]:
        # build a syntax tree for the target so structural comparison is easy
        target_root = SyntaxTreeNode(list(tree.tokens))
        scope_range: tuple[int, int] | None = None
        if scope is not None:
            scope_range = (scope.extent_offset, scope.extent_offset + scope.extent_length)

        heading_paths = _heading_path_index(tree)

        for node in _descend_block_nodes(target_root):
            node_range = _node_byte_range(node, tree.line_starts)
            if node_range is None:
                continue
            if scope_range is not None:
                if not (node_range[0] >= scope_range[0] and node_range[1] <= scope_range[1]):
                    continue
            bindings: dict[str, Any] = {}
            if _node_match(node, pattern.pattern_tree, pattern.placeholders, bindings, tree.source, tree.line_starts):
                start, end = node_range
                yield PatternMatch(
                    node=_MdSymbolRef(
                        kind="match",
                        name_path=tree.source[start:end],
                        extent_offset=start,
                        extent_length=end - start,
                        body_range=None,
                    ),
                    bindings={k: _freeze_binding(v, tree.source, tree.line_starts) for k, v in bindings.items()},
                    symbol_path=_nearest_heading_path(heading_paths, start),
                )

    def render_replacement(
        self, replacement_source: str, bindings: Mapping[str, Any]
    ) -> _MdDeclaration:
        # substitute $name tokens with the captured source strings
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
        return _MdDeclaration(kind="replacement", source=rendered)

    def apply_replacement(
        self, tree: Any, match: PatternMatch, replacement: Any
    ) -> _MdTree:
        if not isinstance(tree, _MdTree):
            raise TypeError(f"tree must be a _MdTree; got {type(tree).__name__}")
        if not isinstance(match.node, _MdSymbolRef):
            raise TypeError(f"match.node must be a _MdSymbolRef; got {type(match.node).__name__}")
        if not isinstance(replacement, _MdDeclaration):
            raise TypeError(f"replacement must be a _MdDeclaration; got {type(replacement).__name__}")
        edit = _Edit(
            offset=match.node.extent_offset,
            length=match.node.extent_length,
            replacement=replacement.source,
        )
        new_source = _apply_edits(tree.source, [edit])
        return self.parse(new_source)

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _MdTree:
        if source_kind != "source_file":
            raise DeclarationError(source_kind, f"markdown has no source kind {source_kind!r}")
        return self.parse("")


# =============================================================================
# Token → symbol walking
# =============================================================================


def _walk_headings(tree: _MdTree) -> Iterator[tuple[str, KindName, _MdSymbolRef]]:
    """Yield headings with hierarchical name paths and body-scope ranges.

    :param tree: the parsed tree.
    :return: iterator of ``(name_path, "heading", ref)`` triples. A heading's
        scope runs from its own start to just before the next heading with the
        same or shallower level.
    """
    # collect heading tokens with their line maps and inline text
    headings = list(_iter_heading_tokens(tree))
    if not headings:
        return
    source = tree.source
    # compute scope ends: the next heading with level <= this heading's level
    n = len(headings)
    for i, (level, text, open_idx, close_idx) in enumerate(headings):
        open_map = tree.tokens[open_idx].map
        close_map = tree.tokens[close_idx].map
        start_line = open_map[0] if open_map else 0
        end_line = close_map[1] if close_map else start_line + 1
        heading_end_offset = tree.line_starts[min(end_line, len(tree.line_starts) - 1)]
        # scope end: start of next equal-or-shallower heading, or EOF
        scope_end_offset = len(source)
        for j in range(i + 1, n):
            nxt_level, _nxt_text, nxt_open_idx, _nxt_close_idx = headings[j]
            if nxt_level <= level:
                nxt_map = tree.tokens[nxt_open_idx].map
                nxt_line = nxt_map[0] if nxt_map else None
                if nxt_line is not None:
                    scope_end_offset = tree.line_starts[min(nxt_line, len(tree.line_starts) - 1)]
                break
        extent_offset = tree.line_starts[min(start_line, len(tree.line_starts) - 1)]
        extent_length = heading_end_offset - extent_offset
        body_range = (heading_end_offset, scope_end_offset) if scope_end_offset > heading_end_offset else None
        # build the hierarchical name path by scanning earlier headings
        path_parts: list[str] = []
        for j in range(i):
            jl, jt, _jopen, _jclose = headings[j]
            # pop deeper or equal-level path parts so only true ancestors remain
            while path_parts and _level_for(path_parts[-1]) >= jl:
                path_parts.pop()
            path_parts.append(_tag_with_level(jt, jl))
            if jl >= level:
                # this heading is not an ancestor of the current one; drop it later
                pass
        # retain only strict ancestors: those with level < current level
        ancestors = [p for p in path_parts if _level_for(p) < level]
        name = text
        full_path = "/".join([_untag(p) for p in ancestors] + [name]) if ancestors else name
        ref = _MdSymbolRef(
            kind="heading",
            name_path=full_path,
            extent_offset=extent_offset,
            extent_length=extent_length,
            body_range=body_range,
        )
        yield full_path, "heading", ref


def _iter_heading_tokens(tree: _MdTree) -> Iterator[tuple[int, str, int, int]]:
    """Yield ``(level, text, open_idx, close_idx)`` for each heading in ``tree``."""
    # markdown-it emits heading_open, inline, heading_close in sequence
    tokens = tree.tokens
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            level = int(tok.tag[1])  # 'h1' → 1
            inline_idx = i + 1
            text = ""
            if inline_idx < len(tokens) and tokens[inline_idx].type == "inline":
                text = (tokens[inline_idx].content or "").strip()
            close_idx = inline_idx + 1
            # advance i past the heading_close
            yield level, text, i, close_idx
            i = close_idx + 1
            continue
        i += 1


def _tag_with_level(text: str, level: int) -> str:
    return f"\x00{level}\x00{text}"


def _untag(tagged: str) -> str:
    if tagged.startswith("\x00"):
        _, _, rest = tagged.partition("\x00")
        _, _, body = rest.partition("\x00")
        return body
    return tagged


def _level_for(tagged: str) -> int:
    if tagged.startswith("\x00"):
        _, _, rest = tagged.partition("\x00")
        lvl, _, _ = rest.partition("\x00")
        return int(lvl)
    return 0


# =============================================================================
# Pattern matching
# =============================================================================


def _encode_sigils(src: str) -> tuple[str, dict[str, _Placeholder]]:
    """Rewrite ``$``-sigils to synthetic identifiers the parser treats as text."""
    # unique counter so placeholders can be distinguished structurally
    placeholders: dict[str, _Placeholder] = {}
    counter = {"n": 0}

    def _replace(match: re.Match[str]) -> str:
        raw_name = match.group("name")
        is_seq = match.group("seq") == "*"
        encoded = f"{_SENTINEL_PREFIX}{counter['n']}"
        counter["n"] += 1
        placeholders[encoded] = _Placeholder(name=raw_name, is_sequence=is_seq, encoded=encoded)
        return encoded

    return _SIGIL_RE.sub(_replace, src), placeholders


def _descend_block_nodes(root: SyntaxTreeNode) -> Iterator[SyntaxTreeNode]:
    """Yield every block-level node in the tree, depth-first."""
    for child in root.children:
        yield child
        yield from _descend_block_nodes(child)


def _node_match(
    target: SyntaxTreeNode,
    pattern: SyntaxTreeNode,
    placeholders: Mapping[str, _Placeholder],
    bindings: dict[str, Any],
    source: str,
    line_starts: tuple[int, ...],
) -> bool:
    """Structurally compare a target node against a pattern node."""
    # placeholder-in-text nodes match any structural subtree
    pat_placeholder = _node_sentinel(pattern, placeholders)
    if pat_placeholder is not None:
        if pat_placeholder.is_sequence:
            return False
        if pat_placeholder.name != "_":
            # capture the target's source range so the caller gets textual bindings
            bindings[pat_placeholder.name] = target
        return True

    if target.type != pattern.type:
        return False

    # text content must match verbatim for inline nodes; block nodes are compared structurally only
    if target.type == "text":
        if (target.content or "") != (pattern.content or ""):
            return False

    target_children = list(target.children)
    pattern_children = list(pattern.children)
    if len(target_children) != len(pattern_children):
        return False
    for tc, pc in zip(target_children, pattern_children, strict=False):
        if not _node_match(tc, pc, placeholders, bindings, source, line_starts):
            return False
    return True


def _node_sentinel(node: SyntaxTreeNode, placeholders: Mapping[str, _Placeholder]) -> _Placeholder | None:
    """Return the placeholder a node represents, if any."""
    # a placeholder sentinel appears as a 'text' node whose content is exactly the sentinel
    if node.type != "text":
        return None
    content = (node.content or "").strip()
    match = _SENTINEL_RE.fullmatch(content)
    if match is None:
        return None
    return placeholders.get(content)


def _node_byte_range(node: SyntaxTreeNode, line_starts: tuple[int, ...]) -> tuple[int, int] | None:
    """Return the ``(start, end)`` byte range of ``node`` in the source."""
    # only block-level nodes carry a line map; inline subtrees do not
    node_map = getattr(node, "map", None)
    if node_map is None:
        return None
    start_line, end_line = node_map
    if start_line < 0 or end_line < 0:
        return None
    start = line_starts[min(start_line, len(line_starts) - 1)]
    end = line_starts[min(end_line, len(line_starts) - 1)]
    return start, end


def _freeze_binding(
    value: Any, source: str, line_starts: tuple[int, ...]
) -> str:
    """Convert a captured SyntaxTreeNode into its source text."""
    if isinstance(value, SyntaxTreeNode):
        rng = _node_byte_range(value, line_starts)
        if rng is not None:
            return source[rng[0] : rng[1]]
        return (value.content or "")
    if isinstance(value, str):
        return value
    return str(value)


def _heading_path_index(tree: _MdTree) -> Sequence[tuple[int, int, str]]:
    """Return ``(start_offset, end_offset, name_path)`` for every heading scope."""
    entries: list[tuple[int, int, str]] = []
    for path, _kind, ref in _walk_headings(tree):
        scope_end = ref.extent_offset + ref.extent_length
        if ref.body_range is not None:
            scope_end = ref.body_range[1]
        entries.append((ref.extent_offset, scope_end, path))
    return entries


def _nearest_heading_path(
    heading_paths: Sequence[tuple[int, int, str]], offset: int
) -> str | None:
    """Return the name_path of the smallest enclosing heading scope for ``offset``."""
    best: tuple[int, str] | None = None  # (scope_size, path)
    for start, end, path in heading_paths:
        if start <= offset < end:
            size = end - start
            if best is None or size < best[0]:
                best = (size, path)
    return best[1] if best is not None else None


# =============================================================================
# Source-edit utilities
# =============================================================================


def _compute_line_starts(source: str) -> tuple[int, ...]:
    """Return cumulative byte offsets of each line start.

    :param source: the source text.
    :return: a tuple of length ``line_count + 1``; the last entry is
        ``len(source)`` so that any ``line_starts[end_line]`` access is safe
        when ``end_line`` points one past the final line.
    """
    # record 0, plus the index after every newline, plus EOF
    starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            starts.append(i + 1)
    if not starts or starts[-1] != len(source):
        starts.append(len(source))
    return tuple(starts)


def _apply_edits(source: str, edits: Sequence[_Edit]) -> str:
    """Apply ``edits`` to ``source`` in reverse-offset order."""
    # earlier-offset edits never shift later ones when applied right-to-left
    out = source
    for edit in sorted(edits, key=lambda e: e.offset, reverse=True):
        out = out[: edit.offset] + edit.replacement + out[edit.offset + edit.length :]
    return out


def _ensure_single_trailing_newline(text: str) -> str:
    """Ensure a block ends with exactly one trailing newline.

    The newline is the block's end-of-line; blank-line separation from adjacent
    blocks is injected by :func:`_block_separators` at insertion time rather
    than baked into every declaration's source.
    """
    if not text:
        return ""
    return text.rstrip("\n") + "\n"


def _block_separators(source: str, offset: int) -> tuple[str, str]:
    """Return the ``(prefix, suffix)`` padding that puts an insertion at a block boundary.

    Each side of the insertion point must sit next to a blank-line boundary:
    the prefix injects whatever newlines are needed so ``source[:offset] +
    prefix`` ends with ``\\n\\n``; the suffix injects what is needed so
    ``suffix + source[offset:]`` begins with ``\\n`` (which, combined with the
    inserted block's own trailing newline, yields a blank line).
    """
    # prefix: walk backwards through existing trailing newlines to see how much padding to add
    prefix = ""
    if offset == 0 or not source:
        prefix = ""
    else:
        prev = source[:offset]
        if prev.endswith("\n\n"):
            prefix = ""
        elif prev.endswith("\n"):
            prefix = "\n"
        else:
            prefix = "\n\n"

    # suffix: append a newline only when the content directly after the insertion
    # point is not itself a newline and not end-of-document
    rest = source[offset:]
    if rest == "" or rest.startswith("\n"):
        suffix = ""
    else:
        suffix = "\n"
    return prefix, suffix


def _apply_list_marker(marker: str, body: str) -> str:
    """Prefix the first line of ``body`` with ``marker`` and indent continuation lines."""
    # indent continuation lines to the marker's width so nested content stays attached to the item
    indent = " " * len(marker)
    lines = body.splitlines()
    if not lines:
        return marker + "\n"
    out = [marker + lines[0]]
    for line in lines[1:]:
        out.append((indent + line) if line else line)
    return "\n".join(out) + "\n"


def _quote_prefix(line: str) -> str:
    """Prefix a single line with '> ', collapsing to '>' when the line is blank."""
    if not line:
        return ">"
    return "> " + line


# =============================================================================
# Attribute helpers
# =============================================================================


def _require_str(attrs: Mapping[str, Any], name: str, kind: KindName) -> str:
    if name not in attrs or attrs[name] is None:
        raise DeclarationError(kind, f"attribute {name!r} is required")
    value = attrs[name]
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be str, got {type(value).__name__}")
    return value


def _require_int(attrs: Mapping[str, Any], name: str, kind: KindName) -> int:
    if name not in attrs or attrs[name] is None:
        raise DeclarationError(kind, f"attribute {name!r} is required")
    value = attrs[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeclarationError(kind, f"attribute {name!r} must be int, got {type(value).__name__}")
    return value


def _require_bool(attrs: Mapping[str, Any], name: str, kind: KindName) -> bool:
    if name not in attrs or attrs[name] is None:
        raise DeclarationError(kind, f"attribute {name!r} is required")
    value = attrs[name]
    if not isinstance(value, bool):
        raise DeclarationError(kind, f"attribute {name!r} must be bool, got {type(value).__name__}")
    return value


def _optional_str_or_none(attrs: Mapping[str, Any], name: str, kind: KindName) -> str | None:
    value = attrs.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be str or None, got {type(value).__name__}")
    return value
