"""JSON :class:`StructuralLanguage` backend.

A hand-rolled round-trip CST for RFC 8259 JSON. The stdlib :mod:`json` module
discards whitespace and formatting, so it cannot satisfy the
``serialize(parse(s)) == s`` invariant the structural framework requires. This
module provides its own minimal lexer, parser, and node types that preserve
every byte of the input.

The CST is intentionally small: six value node kinds (object, array, string,
number, boolean, null), one member node, one document node. Each leaf carries
a :class:`_Tok` that records the exact whitespace and comment text preceding
the token along with the token's own text. Serialization is a straight
concatenation of ``tok.pre_ws + tok.text`` in source order, plus the
document's trailing whitespace.

The CST underpins the full :class:`StructuralLanguage` surface declared at
the bottom of this module: parse / serialize, kind schema, logical-name
resolution, symbol walk, declaration construction, insert / remove, pattern
matching, and rewriting.
"""

from __future__ import annotations

import copy
import json as _stdlib_json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.errors import DeclarationError, NameResolutionError, ParseError, PatternError
from solidlsp.structural.kinds import AttributeSpec, KindName, KindSchema, KindSpec
from solidlsp.structural.names import LogicalName, LogicalNameResolver, NameResolution
from solidlsp.structural.patterns import AstPattern, PatternMatch

# --------------------------------------------------------------------------- #
# Tokens and CST nodes
# --------------------------------------------------------------------------- #


@dataclass
class _Tok:
    """A single lexical token with the whitespace that preceded it.

    ``pre_ws`` captures everything between the end of the previous token and
    the start of this one (whitespace only; JSON has no comments). The
    previous token's trailing side is implicit: it ends the moment a new
    token's ``pre_ws`` begins. Concatenating every token's ``pre_ws + text``
    in source order, followed by the document's ``trailing_ws``, reproduces
    the input byte-for-byte.
    """

    pre_ws: str
    text: str


@dataclass
class _JsonNode:
    """Base class for JSON CST nodes. Present only as a nominal type."""


@dataclass
class _JsonString(_JsonNode):
    r"""A JSON string literal, including surrounding quotes and escapes.

    ``tok.text`` is the raw slice from the source, e.g. ``"\"hello\\n\""``.
    No decoding is performed; the surface syntax is what the backend preserves.
    """

    tok: _Tok


@dataclass
class _JsonNumber(_JsonNode):
    """A JSON number literal, preserving its exact textual form."""

    tok: _Tok


@dataclass
class _JsonBool(_JsonNode):
    """A JSON ``true`` or ``false`` literal."""

    tok: _Tok


@dataclass
class _JsonNull(_JsonNode):
    """A JSON ``null`` literal."""

    tok: _Tok


@dataclass
class _JsonMember(_JsonNode):
    """A member of a JSON object: a string key, a colon, and a value."""

    key: _JsonString
    colon: _Tok
    value: _JsonNode


@dataclass
class _JsonObject(_JsonNode):
    """A JSON object: ``{`` ..members.. ``}``.

    ``commas`` holds the comma separators in source order. A strictly
    conforming JSON object has ``len(commas) == len(members) - 1`` when
    non-empty, and ``len(commas) == 0`` when empty. Trailing commas are not
    permitted by RFC 8259; if the parser sees one it raises
    :class:`ParseError`.
    """

    open: _Tok
    members: list[_JsonMember] = field(default_factory=list)
    commas: list[_Tok] = field(default_factory=list)
    close: _Tok = field(default_factory=lambda: _Tok("", "}"))


@dataclass
class _JsonArray(_JsonNode):
    """A JSON array: ``[`` ..items.. ``]``."""

    open: _Tok
    items: list[_JsonNode] = field(default_factory=list)
    commas: list[_Tok] = field(default_factory=list)
    close: _Tok = field(default_factory=lambda: _Tok("", "]"))


@dataclass
class _JsonDocument(_JsonNode):
    """A complete JSON document: one root value plus trailing whitespace."""

    root: _JsonNode
    trailing_ws: str = ""


# --------------------------------------------------------------------------- #
# Lexer
# --------------------------------------------------------------------------- #


_WHITESPACE = frozenset(" \t\n\r")
_STRUCT_CHARS = frozenset("{}[]:,")
_NUMBER_START = frozenset("-0123456789")
_DIGIT = frozenset("0123456789")


class _Lexer:
    """Stream lexer that yields :class:`_Tok` instances plus final whitespace.

    The lexer is character-by-character rather than regex-based so that the
    parse error it raises on malformed input can cite an exact byte offset.
    """

    def __init__(self, source: str) -> None:
        # source state
        self._src = source
        self._pos = 0

    def _peek(self) -> str:
        return self._src[self._pos] if self._pos < len(self._src) else ""

    def _advance(self) -> str:
        # advance one char and return it
        ch = self._src[self._pos]
        self._pos += 1
        return ch

    def _consume_whitespace(self) -> str:
        # collect a run of whitespace chars
        start = self._pos
        while self._pos < len(self._src) and self._src[self._pos] in _WHITESPACE:
            self._pos += 1
        return self._src[start : self._pos]

    def _consume_string(self) -> str:
        # consume a quoted string including escapes; return the raw slice
        start = self._pos
        assert self._src[self._pos] == '"'
        self._pos += 1  # opening quote
        while self._pos < len(self._src):
            ch = self._src[self._pos]
            if ch == "\\":
                # escape consumes the next char unconditionally; JSON permits
                # \" \\ \/ \b \f \n \r \t \uXXXX — we do not validate the
                # specific escape here, only that one character follows.
                self._pos += 2
                continue
            if ch == '"':
                self._pos += 1  # closing quote
                return self._src[start : self._pos]
            if ord(ch) < 0x20:
                raise ParseError(
                    "json",
                    self._src[max(0, self._pos - 20) : self._pos + 20],
                    f"unescaped control character U+{ord(ch):04X} in string at byte {self._pos}",
                )
            self._pos += 1
        raise ParseError(
            "json",
            self._src[start : min(len(self._src), start + 40)],
            f"unterminated string starting at byte {start}",
        )

    def _consume_number(self) -> str:
        # consume a numeric literal (int, frac, exp) per RFC 8259
        start = self._pos
        if self._peek() == "-":
            self._advance()
        # integer part
        if self._peek() == "0":
            self._advance()
        elif self._peek() in _DIGIT:
            while self._peek() in _DIGIT:
                self._advance()
        else:
            raise ParseError(
                "json",
                self._src[max(0, start - 10) : start + 10],
                f"expected digit after sign at byte {self._pos}",
            )
        # fraction
        if self._peek() == ".":
            self._advance()
            if self._peek() not in _DIGIT:
                raise ParseError(
                    "json",
                    self._src[max(0, start - 10) : self._pos + 10],
                    f"expected digit after decimal point at byte {self._pos}",
                )
            while self._peek() in _DIGIT:
                self._advance()
        # exponent
        if self._peek() and self._peek() in "eE":
            self._advance()
            if self._peek() and self._peek() in "+-":
                self._advance()
            if self._peek() not in _DIGIT:
                raise ParseError(
                    "json",
                    self._src[max(0, start - 10) : self._pos + 10],
                    f"expected digit in exponent at byte {self._pos}",
                )
            while self._peek() in _DIGIT:
                self._advance()
        return self._src[start : self._pos]

    def _consume_keyword(self, keyword: str) -> str:
        # consume an exact keyword (true/false/null); raise if absent
        if self._src.startswith(keyword, self._pos):
            self._pos += len(keyword)
            return keyword
        raise ParseError(
            "json",
            self._src[max(0, self._pos - 10) : self._pos + 10],
            f"expected {keyword!r} at byte {self._pos}",
        )

    def next_token(self) -> _Tok | None:
        """Return the next token, or ``None`` if only trailing whitespace remains.

        The caller must call :meth:`trailing_whitespace` exactly once after
        :meth:`next_token` returns ``None`` to recover the document-final
        whitespace.
        """
        pre = self._consume_whitespace()
        if self._pos >= len(self._src):
            # save residual pre_ws for trailing collection
            self._trailing = pre
            return None
        ch = self._src[self._pos]
        if ch in _STRUCT_CHARS:
            self._pos += 1
            return _Tok(pre, ch)
        if ch == '"':
            return _Tok(pre, self._consume_string())
        if ch in _NUMBER_START:
            return _Tok(pre, self._consume_number())
        if ch == "t":
            return _Tok(pre, self._consume_keyword("true"))
        if ch == "f":
            return _Tok(pre, self._consume_keyword("false"))
        if ch == "n":
            return _Tok(pre, self._consume_keyword("null"))
        raise ParseError(
            "json",
            self._src[max(0, self._pos - 10) : self._pos + 10],
            f"unexpected character {ch!r} at byte {self._pos}",
        )

    def trailing_whitespace(self) -> str:
        """Return the document-trailing whitespace captured by the last
        :meth:`next_token` call that returned ``None``.
        """
        return getattr(self, "_trailing", "")


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class _Parser:
    """Recursive-descent parser producing a :class:`_JsonDocument`."""

    def __init__(self, source: str) -> None:
        # lexer + one-token lookahead buffer
        self._lex = _Lexer(source)
        self._peeked: _Tok | None = None
        self._exhausted = False

    def _peek(self) -> _Tok | None:
        if self._peeked is None and not self._exhausted:
            nxt = self._lex.next_token()
            if nxt is None:
                self._exhausted = True
            else:
                self._peeked = nxt
        return self._peeked

    def _take(self) -> _Tok:
        tok = self._peek()
        if tok is None:
            raise ParseError("json", "", "unexpected end of input")
        self._peeked = None
        return tok

    def parse(self) -> _JsonDocument:
        # a document is exactly one value followed by optional trailing whitespace
        root = self._parse_value()
        if self._peek() is not None:
            extra = self._take()
            raise ParseError(
                "json",
                extra.text,
                f"trailing content after document root: {extra.text!r}",
            )
        return _JsonDocument(root=root, trailing_ws=self._lex.trailing_whitespace())

    def _parse_value(self) -> _JsonNode:
        # dispatch based on the first token's shape
        tok = self._peek()
        if tok is None:
            raise ParseError("json", "", "expected value, got end of input")
        text = tok.text
        if text == "{":
            return self._parse_object()
        if text == "[":
            return self._parse_array()
        if text.startswith('"'):
            return _JsonString(self._take())
        if text in ("true", "false"):
            return _JsonBool(self._take())
        if text == "null":
            return _JsonNull(self._take())
        # anything else is treated as a number; the lexer already validated form
        return _JsonNumber(self._take())

    def _parse_object(self) -> _JsonObject:
        # consume '{' then zero or more members separated by commas, then '}'
        open_tok = self._take()
        assert open_tok.text == "{"
        obj = _JsonObject(open=open_tok, close=_Tok("", "}"))
        nxt = self._peek()
        if nxt is not None and nxt.text == "}":
            obj.close = self._take()
            return obj
        while True:
            obj.members.append(self._parse_member())
            nxt = self._peek()
            if nxt is None:
                raise ParseError("json", "", "unterminated object")
            if nxt.text == ",":
                obj.commas.append(self._take())
                continue
            if nxt.text == "}":
                obj.close = self._take()
                return obj
            raise ParseError(
                "json",
                nxt.text,
                f"expected ',' or '}}' in object, got {nxt.text!r}",
            )

    def _parse_member(self) -> _JsonMember:
        # key ':' value
        key_tok = self._peek()
        if key_tok is None or not key_tok.text.startswith('"'):
            got = "end of input" if key_tok is None else repr(key_tok.text)
            raise ParseError(
                "json",
                "" if key_tok is None else key_tok.text,
                f"expected string key in object, got {got}",
            )
        key = _JsonString(self._take())
        colon = self._peek()
        if colon is None or colon.text != ":":
            got = "end of input" if colon is None else repr(colon.text)
            raise ParseError(
                "json",
                "" if colon is None else colon.text,
                f"expected ':' after object key, got {got}",
            )
        colon_tok = self._take()
        value = self._parse_value()
        return _JsonMember(key=key, colon=colon_tok, value=value)

    def _parse_array(self) -> _JsonArray:
        # consume '[' then zero or more values separated by commas, then ']'
        open_tok = self._take()
        assert open_tok.text == "["
        arr = _JsonArray(open=open_tok, close=_Tok("", "]"))
        nxt = self._peek()
        if nxt is not None and nxt.text == "]":
            arr.close = self._take()
            return arr
        while True:
            arr.items.append(self._parse_value())
            nxt = self._peek()
            if nxt is None:
                raise ParseError("json", "", "unterminated array")
            if nxt.text == ",":
                arr.commas.append(self._take())
                continue
            if nxt.text == "]":
                arr.close = self._take()
                return arr
            raise ParseError(
                "json",
                nxt.text,
                f"expected ',' or ']' in array, got {nxt.text!r}",
            )


# --------------------------------------------------------------------------- #
# Serializer
# --------------------------------------------------------------------------- #


def _emit(node: _JsonNode, out: list[str]) -> None:
    """Append every ``pre_ws + text`` token from ``node`` to ``out`` in order."""
    if isinstance(node, _JsonDocument):
        _emit(node.root, out)
        out.append(node.trailing_ws)
        return
    if isinstance(node, _JsonString | _JsonNumber | _JsonBool | _JsonNull):
        out.append(node.tok.pre_ws)
        out.append(node.tok.text)
        return
    if isinstance(node, _JsonMember):
        _emit(node.key, out)
        out.append(node.colon.pre_ws)
        out.append(node.colon.text)
        _emit(node.value, out)
        return
    if isinstance(node, _JsonObject):
        out.append(node.open.pre_ws)
        out.append(node.open.text)
        for i, member in enumerate(node.members):
            _emit(member, out)
            if i < len(node.commas):
                out.append(node.commas[i].pre_ws)
                out.append(node.commas[i].text)
        out.append(node.close.pre_ws)
        out.append(node.close.text)
        return
    if isinstance(node, _JsonArray):
        out.append(node.open.pre_ws)
        out.append(node.open.text)
        for i, item in enumerate(node.items):
            _emit(item, out)
            if i < len(node.commas):
                out.append(node.commas[i].pre_ws)
                out.append(node.commas[i].text)
        out.append(node.close.pre_ws)
        out.append(node.close.text)
        return
    raise TypeError(f"unknown JSON CST node type: {type(node).__name__}")


# --------------------------------------------------------------------------- #
# Module-level convenience
# --------------------------------------------------------------------------- #


def parse_json(source: str) -> _JsonDocument:
    """Parse ``source`` into a round-trip-faithful JSON CST.

    :raises ParseError: on any malformed input.
    """
    return _Parser(source).parse()


def serialize_json(tree: Any) -> str:
    """Serialize a JSON CST node back to its source text.

    The invariant ``serialize_json(parse_json(s)) == s`` holds for every input
    :func:`parse_json` accepts.
    """
    parts: list[str] = []
    _emit(tree, parts)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Kind schema (stage 2)
# --------------------------------------------------------------------------- #


_ATTR_KEY = AttributeSpec(
    name="key",
    type_hint="str",
    required=True,
    description="object member key, already unescaped; the backend quotes and escapes it on serialization",
)
_ATTR_RAW_TEXT = AttributeSpec(
    name="raw_text",
    type_hint="str",
    required=True,
    description="verbatim source text of the literal (string with surrounding quotes and escapes, or number text as written)",
)
_ATTR_BOOL_VALUE = AttributeSpec(
    name="value",
    type_hint="bool",
    required=True,
    description="True for ``true``, False for ``false``",
)


def json_kind_schema() -> KindSchema:
    """Return the JSON structural kind vocabulary.

    :return: the kind schema exposed by :class:`JsonStructuralLanguage`.
    """
    # scalar-value kinds (every value that may appear directly under a document, array, or member)
    _VALUE_KINDS = frozenset({"object", "array", "string", "number", "boolean", "null"})
    _VALUE_PARENT_KINDS = frozenset({"document", "array", "member"})

    # root: the document wraps exactly one value
    document = KindSpec(
        name="document",
        description="A JSON document's top level; wraps exactly one root value.",
        attributes=(),
        allowed_parent_kinds=frozenset(),
        allowed_child_kinds=_VALUE_KINDS,
    )

    # container: object (composed of members)
    obj = KindSpec(
        name="object",
        description="A JSON object ``{`` ..members.. ``}``.",
        attributes=(),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=frozenset({"member"}),
    )

    # container: array (composed of values)
    array = KindSpec(
        name="array",
        description="A JSON array ``[`` ..values.. ``]``.",
        attributes=(),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=_VALUE_KINDS,
    )

    # member: key-value pair within an object
    member = KindSpec(
        name="member",
        description="A ``key: value`` pair within a JSON object.",
        attributes=(_ATTR_KEY,),
        allowed_parent_kinds=frozenset({"object"}),
        allowed_child_kinds=_VALUE_KINDS,
    )

    # leaf: string literal
    string = KindSpec(
        name="string",
        description="A JSON string literal.",
        attributes=(_ATTR_RAW_TEXT,),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=frozenset(),
    )

    # leaf: number literal
    number = KindSpec(
        name="number",
        description="A JSON number literal.",
        attributes=(_ATTR_RAW_TEXT,),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=frozenset(),
    )

    # leaf: boolean literal
    boolean = KindSpec(
        name="boolean",
        description="A JSON ``true`` or ``false`` literal.",
        attributes=(_ATTR_BOOL_VALUE,),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=frozenset(),
    )

    # leaf: null literal
    null = KindSpec(
        name="null",
        description="A JSON ``null`` literal.",
        attributes=(),
        allowed_parent_kinds=_VALUE_PARENT_KINDS,
        allowed_child_kinds=frozenset(),
    )

    return KindSchema(
        language_key="json",
        source_kinds=frozenset({"document"}),
        kinds={
            "document": document,
            "object": obj,
            "array": array,
            "member": member,
            "string": string,
            "number": number,
            "boolean": boolean,
            "null": null,
        },
    )


_JSON_KIND_SCHEMA = json_kind_schema()


# --------------------------------------------------------------------------- #
# Logical name resolution (stage 2)
# --------------------------------------------------------------------------- #


_JSON_NAME_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*")


class JsonLogicalNameResolver(LogicalNameResolver):
    """Maps a slashed or dotted logical name to a ``.json`` file under a root.

    The resolver is file-level: it identifies which ``.json`` file a logical
    name points at. Inside-file navigation (into nested members or array
    elements) is handled by :func:`walk_symbols`, not by this resolver.

    :ivar _project_root: project root; resolutions report paths relative to it.
    :ivar _source_roots: ordered content directories to probe.
    :ivar _extensions: file extensions to try, in order.
    """

    _DEFAULT_EXTENSIONS: tuple[str, ...] = (".json",)

    def __init__(
        self,
        project_root: Path,
        source_roots: Sequence[Path] = (),
        extensions: Sequence[str] = (),
    ):
        """:param project_root: directory at whose root paths are reported.
        :param source_roots: directories under which ``.json`` files live;
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
        # grammar: slash- or dot-separated path; each part is a filename token
        if not raw:
            raise NameResolutionError(raw, "empty logical name")
        parts = tuple(re.split(r"[./]", raw))
        for part in parts:
            if not part or not _JSON_NAME_PART.fullmatch(part):
                raise NameResolutionError(raw, f"invalid json name part: {part!r}")
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
        return NameResolution(relative_path=str(relative), source_kind="document", exists=exists)


# --------------------------------------------------------------------------- #
# Symbol walking (stage 2)
# --------------------------------------------------------------------------- #


def _value_kind(node: _JsonNode) -> KindName:
    """Return the schema kind name for a JSON value node."""
    if isinstance(node, _JsonObject):
        return "object"
    if isinstance(node, _JsonArray):
        return "array"
    if isinstance(node, _JsonString):
        return "string"
    if isinstance(node, _JsonNumber):
        return "number"
    if isinstance(node, _JsonBool):
        return "boolean"
    if isinstance(node, _JsonNull):
        return "null"
    raise TypeError(f"unexpected JSON value node type: {type(node).__name__}")


def _decode_key(member: _JsonMember) -> str:
    """Decode a member's key string literal to its unescaped text.

    The key is stored as a :class:`_JsonString` preserving its quotes and
    escapes. This helper returns the semantic key a path segment should use.
    """
    return _stdlib_json.loads(member.key.tok.text)


def _walk(node: _JsonNode, prefix: str) -> Iterable[tuple[str, KindName, _JsonNode]]:
    """Yield addressable symbols under ``node`` with name paths built on ``prefix``.

    The walk yields every object member and every array element. Members
    whose values are compound (objects, arrays) are walked recursively so
    nested members and elements are also addressable. Scalar values directly
    under a member are not yielded separately \u2014 the member itself is the
    addressable symbol.
    """
    # dispatch: object members / array elements / leaves
    if isinstance(node, _JsonObject):
        for member in node.members:
            # slash-join the member key with the current prefix
            key = _decode_key(member)
            member_path = f"{prefix}/{key}" if prefix else key
            yield member_path, "member", member
            yield from _walk(member.value, member_path)
        return

    if isinstance(node, _JsonArray):
        for index, item in enumerate(node.items):
            # array indices take the bracketed form and extend the path verbatim
            segment = f"[{index}]"
            item_path = f"{prefix}/{segment}" if prefix else segment
            yield item_path, _value_kind(item), item
            yield from _walk(item, item_path)
        return

    # scalars contribute no addressable symbols of their own
    return


def walk_symbols(tree: _JsonDocument) -> Iterable[tuple[str, KindName, _JsonNode]]:
    """Yield ``(name_path, kind, node)`` for every addressable JSON symbol in ``tree``.

    Object members and array elements are addressable. Name paths are
    slash-separated; array elements use ``[N]`` segments. The root document
    and its root value carry no name path and are not yielded; only members
    and elements below them are.

    :raises TypeError: if ``tree`` is not a :class:`_JsonDocument`.
    """
    if not isinstance(tree, _JsonDocument):
        raise TypeError(f"walk_symbols expects a _JsonDocument, got {type(tree).__name__}")
    return list(_walk(tree.root, ""))


# --------------------------------------------------------------------------- #
# Patterns (stage 3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _JsonPattern:
    """Compiled JSON pattern.

    The pattern source is parsed as JSON. Two sigils are recognised inside
    string literals:

    * ``"$_"`` \u2014 anonymous wildcard that matches any value at that position.
    * ``"$name"`` \u2014 named capture (``name`` is a simple identifier) that
      matches any value and binds it to ``name``.

    Any other string literal matches literally. Numbers, booleans and nulls
    match literally on their source text; objects and arrays match
    structurally (same members in source order; recursive value match).
    """

    source: str
    tree: _JsonDocument


@dataclass(frozen=True)
class _JsonReplacement:
    """Rendered replacement subtree ready for :meth:`JsonStructuralLanguage.apply_replacement`."""

    node: _JsonNode


_CAPTURE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _decoded_string(node: _JsonString) -> str:
    """Return the unescaped Python string carried by a ``_JsonString`` literal."""
    return _stdlib_json.loads(node.tok.text)


def _capture_kind(literal: str) -> tuple[str, str] | None:
    """Classify a decoded string literal as a capture sigil.

    :return: ``("wildcard", "")`` for ``$_``, ``("named", name)`` for ``$name``
        when ``name`` is a simple identifier, or ``None`` otherwise.
    """
    if literal == "$_":
        return ("wildcard", "")
    if literal.startswith("$") and _CAPTURE_NAME.fullmatch(literal[1:]):
        return ("named", literal[1:])
    return None


def _iter_value_nodes(node: _JsonNode) -> Iterator[_JsonNode]:
    """Yield ``node`` and every value node nested inside it in source order."""
    yield node
    if isinstance(node, _JsonObject):
        for member in node.members:
            yield from _iter_value_nodes(member.value)
    elif isinstance(node, _JsonArray):
        for item in node.items:
            yield from _iter_value_nodes(item)


def _match_value(pattern: _JsonNode, target: _JsonNode, bindings: dict[str, _JsonNode]) -> bool:
    """Structurally match ``pattern`` against ``target``.

    Mutates ``bindings`` when a named capture succeeds. Capture sigils in the
    pattern match any target value regardless of kind.
    """
    # capture sigils take priority over literal matching
    if isinstance(pattern, _JsonString):
        capture = _capture_kind(_decoded_string(pattern))
        if capture is not None:
            kind, name = capture
            if kind == "named":
                bindings[name] = target
            return True

    # non-capture pattern must share the target's value kind
    if type(pattern) is not type(target):
        return False

    if isinstance(pattern, _JsonString):
        assert isinstance(target, _JsonString)
        return _decoded_string(pattern) == _decoded_string(target)

    if isinstance(pattern, _JsonNumber):
        assert isinstance(target, _JsonNumber)
        return pattern.tok.text == target.tok.text

    if isinstance(pattern, _JsonBool):
        assert isinstance(target, _JsonBool)
        return pattern.tok.text == target.tok.text

    if isinstance(pattern, _JsonNull):
        return True

    if isinstance(pattern, _JsonObject):
        assert isinstance(target, _JsonObject)
        if len(pattern.members) != len(target.members):
            return False
        for p_member, t_member in zip(pattern.members, target.members, strict=True):
            if _decoded_string(p_member.key) != _decoded_string(t_member.key):
                return False
            if not _match_value(p_member.value, t_member.value, bindings):
                return False
        return True

    if isinstance(pattern, _JsonArray):
        assert isinstance(target, _JsonArray)
        if len(pattern.items) != len(target.items):
            return False
        for p_item, t_item in zip(pattern.items, target.items, strict=True):
            if not _match_value(p_item, t_item, bindings):
                return False
        return True

    return False


def _render_capture_resolution(node: _JsonNode, bindings: Mapping[str, _JsonNode]) -> _JsonNode:
    """Return ``node`` with capture sigils substituted from ``bindings``.

    :raises PatternError: when a ``$_`` wildcard or unbound ``$name`` appears
        in the replacement source.
    """
    # string capture sigils become the matching captured subtree
    if isinstance(node, _JsonString):
        capture = _capture_kind(_decoded_string(node))
        if capture is None:
            return node
        kind, name = capture
        if kind == "wildcard":
            raise PatternError("parse", "replacement source contains $_ which has no binding")
        if name not in bindings:
            raise PatternError("parse", f"replacement references capture {name!r} with no binding")
        # clear the captured node's pre_ws so the surrounding template controls placement
        substituted = copy.deepcopy(bindings[name])
        _clear_leading_ws(substituted)
        return substituted

    # compound nodes re-thread capture-resolved children into fresh wrappers
    if isinstance(node, _JsonObject):
        new_members = [
            _JsonMember(
                key=m.key,
                colon=m.colon,
                value=_render_capture_resolution(m.value, bindings),
            )
            for m in node.members
        ]
        return _JsonObject(open=node.open, members=new_members, commas=list(node.commas), close=node.close)

    if isinstance(node, _JsonArray):
        new_items = [_render_capture_resolution(item, bindings) for item in node.items]
        return _JsonArray(open=node.open, items=new_items, commas=list(node.commas), close=node.close)

    return node


def _replace_in_tree(node: _JsonNode, target: _JsonNode, replacement: _JsonNode) -> tuple[_JsonNode, bool]:
    """Return a copy of ``node`` with ``target`` (matched by identity) replaced.

    :return: ``(new_node, found)`` where ``found`` is ``True`` iff ``target``
        was located and replaced in the subtree rooted at ``node``.
    """
    # identity match: splice in a fresh replacement copy
    if node is target:
        return copy.deepcopy(replacement), True

    if isinstance(node, _JsonDocument):
        new_root, found = _replace_in_tree(node.root, target, replacement)
        if found:
            return _JsonDocument(root=new_root, trailing_ws=node.trailing_ws), True
        return node, False

    if isinstance(node, _JsonMember):
        new_value, found = _replace_in_tree(node.value, target, replacement)
        if found:
            return _JsonMember(key=node.key, colon=node.colon, value=new_value), True
        return node, False

    if isinstance(node, _JsonObject):
        for i, member in enumerate(node.members):
            new_member, found = _replace_in_tree(member, target, replacement)
            if found:
                assert isinstance(new_member, _JsonMember)
                new_members = list(node.members)
                new_members[i] = new_member
                return _JsonObject(open=node.open, members=new_members, commas=list(node.commas), close=node.close), True
        return node, False

    if isinstance(node, _JsonArray):
        for i, item in enumerate(node.items):
            new_item, found = _replace_in_tree(item, target, replacement)
            if found:
                new_items = list(node.items)
                new_items[i] = new_item
                return _JsonArray(open=node.open, items=new_items, commas=list(node.commas), close=node.close), True
        return node, False

    return node, False


# --------------------------------------------------------------------------- #
# Attribute helpers (stage 3)
# --------------------------------------------------------------------------- #


def _require_str(attributes: Mapping[str, Any], name: str, kind: str) -> str:
    """Read a required string attribute from a declaration."""
    if name not in attributes:
        raise DeclarationError(kind, f"missing required attribute {name!r}")
    value = attributes[name]
    if not isinstance(value, str):
        raise DeclarationError(kind, f"attribute {name!r} must be str, got {type(value).__name__}")
    return value


def _require_bool(attributes: Mapping[str, Any], name: str, kind: str) -> bool:
    """Read a required boolean attribute from a declaration."""
    if name not in attributes:
        raise DeclarationError(kind, f"missing required attribute {name!r}")
    value = attributes[name]
    if not isinstance(value, bool):
        raise DeclarationError(kind, f"attribute {name!r} must be bool, got {type(value).__name__}")
    return value


def _validate_string_literal(raw_text: str, kind: str) -> None:
    """Confirm a raw JSON string literal parses and unescapes cleanly."""
    if not (raw_text.startswith('"') and raw_text.endswith('"') and len(raw_text) >= 2):
        raise DeclarationError(kind, "string raw_text must be enclosed in double quotes")
    try:
        decoded = _stdlib_json.loads(raw_text)
    except _stdlib_json.JSONDecodeError as err:
        raise DeclarationError(kind, f"string raw_text is not a valid JSON string literal: {err.msg}") from err
    if not isinstance(decoded, str):
        raise DeclarationError(kind, f"string raw_text did not decode to a string; got {type(decoded).__name__}")


def _validate_number_literal(raw_text: str, kind: str) -> None:
    """Confirm a raw JSON number literal matches the RFC 8259 grammar."""
    try:
        decoded = _stdlib_json.loads(raw_text)
    except _stdlib_json.JSONDecodeError as err:
        raise DeclarationError(kind, f"number raw_text is not a valid JSON number literal: {err.msg}") from err
    if not isinstance(decoded, int | float) or isinstance(decoded, bool):
        raise DeclarationError(kind, f"number raw_text did not decode to a number; got {type(decoded).__name__}")


# --------------------------------------------------------------------------- #
# Mutation helpers (stage 3)
# --------------------------------------------------------------------------- #


def _clear_leading_ws(node: _JsonNode) -> None:
    """Clear the leading whitespace of ``node``'s first emitted token."""
    tok = _leading_token(node)
    if tok is not None:
        tok.pre_ws = ""


def _leading_token(node: _JsonNode) -> _Tok | None:
    """Return the first emitted token of ``node``, or ``None`` for a document."""
    if isinstance(node, _JsonString | _JsonNumber | _JsonBool | _JsonNull):
        return node.tok
    if isinstance(node, _JsonObject):
        return node.open
    if isinstance(node, _JsonArray):
        return node.open
    if isinstance(node, _JsonMember):
        return node.key.tok
    return None


def _find_member_index(obj: _JsonObject, anchor: Any) -> int:
    """Return the index of ``anchor`` in ``obj.members`` matched by identity."""
    if not isinstance(anchor, _JsonMember):
        raise TypeError(f"object anchor must be a _JsonMember, got {type(anchor).__name__}")
    for i, m in enumerate(obj.members):
        if m is anchor:
            return i
    raise ValueError("anchor not found under parent object")


def _find_item_index(arr: _JsonArray, anchor: Any) -> int:
    """Return the index of ``anchor`` in ``arr.items`` matched by identity."""
    if not isinstance(anchor, _JsonNode):
        raise TypeError(f"array anchor must be a _JsonNode, got {type(anchor).__name__}")
    for i, item in enumerate(arr.items):
        if item is anchor:
            return i
    raise ValueError("anchor not found under parent array")


def _object_with_inserted(
    obj: _JsonObject,
    child: _JsonMember,
    anchor: Any,
    position: str,
) -> _JsonObject:
    """Return a copy of ``obj`` with ``child`` inserted at ``anchor`` / ``position``."""
    # child has already been deep-copied by the caller; clear its pre_ws so we control placement
    _clear_leading_ws(child)
    members = list(obj.members)  # shallow copy: existing member nodes stay shared and untouched
    if anchor is None:
        index = 0 if position == "start" else len(members)
    else:
        anchor_index = _find_member_index(obj, anchor)
        index = anchor_index if position in {"before", "start"} else anchor_index + 1
    members.insert(index, child)
    commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(members) - 1))]
    return _JsonObject(open=obj.open, members=members, commas=commas, close=obj.close)


def _array_with_inserted(
    arr: _JsonArray,
    child: _JsonNode,
    anchor: Any,
    position: str,
) -> _JsonArray:
    """Return a copy of ``arr`` with ``child`` inserted at ``anchor`` / ``position``."""
    _clear_leading_ws(child)
    items = list(arr.items)
    if anchor is None:
        index = 0 if position == "start" else len(items)
    else:
        anchor_index = _find_item_index(arr, anchor)
        index = anchor_index if position in {"before", "start"} else anchor_index + 1
    items.insert(index, child)
    commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(items) - 1))]
    return _JsonArray(open=arr.open, items=items, commas=commas, close=arr.close)


def _object_without(obj: _JsonObject, child: Any) -> _JsonObject:
    """Return a copy of ``obj`` without ``child`` (matched by identity)."""
    if not isinstance(child, _JsonMember):
        raise TypeError(f"object child to remove must be a _JsonMember, got {type(child).__name__}")
    for i, m in enumerate(obj.members):
        if m is child:
            members = [x for j, x in enumerate(obj.members) if j != i]
            commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(members) - 1))]
            return _JsonObject(open=obj.open, members=members, commas=commas, close=obj.close)
    raise ValueError("child not found under parent object")


def _array_without(arr: _JsonArray, child: Any) -> _JsonArray:
    """Return a copy of ``arr`` without ``child`` (matched by identity)."""
    if not isinstance(child, _JsonNode):
        raise TypeError(f"array child to remove must be a _JsonNode, got {type(child).__name__}")
    for i, item in enumerate(arr.items):
        if item is child:
            items = [x for j, x in enumerate(arr.items) if j != i]
            commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(items) - 1))]
            return _JsonArray(open=arr.open, items=items, commas=commas, close=arr.close)
    raise ValueError("child not found under parent array")


# --------------------------------------------------------------------------- #
# Container-member editing helpers
# --------------------------------------------------------------------------- #


def _split_member_path_json(member_path: str) -> tuple[str, str]:
    """Split ``foo/bar/[0]`` into ``("foo/bar", "[0]")`` etc.

    Returns ``("", <first>)`` when ``member_path`` has no ``/`` — in that
    case the member is a direct child of the document root.
    """
    slash_idx = member_path.rfind("/")
    if slash_idx < 0:
        return "", member_path
    return member_path[:slash_idx], member_path[slash_idx + 1 :]


def _root_as_container(tree: _JsonDocument) -> _JsonObject | _JsonArray:
    """Return the document root if it is a container; raise otherwise."""
    root = tree.root
    if not isinstance(root, _JsonObject | _JsonArray):
        raise ValueError(f"document root is {_value_kind(root)!r}, not a container")
    return root


def _resolve_container(tree: _JsonDocument, container_path: str) -> _JsonObject | _JsonArray:
    """Resolve ``container_path`` to a ``_JsonObject``/``_JsonArray`` inside ``tree``.

    Empty path resolves to the document root.
    """
    if not container_path:
        return _root_as_container(tree)
    node = _resolve_node_by_path(tree, container_path)
    if isinstance(node, _JsonObject | _JsonArray):
        return node
    # members wrap their value; when the path points at a member whose value is a container, descend
    if isinstance(node, _JsonMember) and isinstance(node.value, _JsonObject | _JsonArray):
        return node.value
    raise ValueError(
        f"path {container_path!r} does not resolve to a JSON object or array",
    )


def _resolve_node_by_path(tree: _JsonDocument, path: str) -> _JsonNode:
    """Walk ``tree`` along ``path`` and return the matching node.

    For object paths, returns the member's VALUE (not the ``_JsonMember``
    wrapper) — matching what :func:`walk_symbols` yields for member paths.
    For array paths returns the item directly.
    """
    if not path:
        # empty path is not a valid member/container target
        raise ValueError("empty path does not resolve to a node")
    node: _JsonNode = tree.root
    for segment in path.split("/"):
        if segment.startswith("[") and segment[-1:] == "]":
            # array index segment
            if not isinstance(node, _JsonArray):
                raise ValueError(f"segment {segment!r} expects an array, got {_value_kind(node)!r}")
            try:
                idx = int(segment[1:-1])
            except ValueError as err:
                raise ValueError(f"segment {segment!r} is not a valid array index") from err
            if idx < 0 or idx >= len(node.items):
                raise ValueError(f"array index {idx} out of range for segment {segment!r}")
            node = node.items[idx]
        else:
            # object key segment
            if not isinstance(node, _JsonObject):
                raise ValueError(f"segment {segment!r} expects an object, got {_value_kind(node)!r}")
            match = next((m for m in node.members if _decode_key(m) == segment), None)
            if match is None:
                raise ValueError(f"object has no member with key {segment!r}")
            node = match.value
    return node


def _find_member_for_value(obj: _JsonObject, value: _JsonNode) -> _JsonMember:
    """Locate the member within ``obj`` whose ``.value`` is identity-equal to ``value``."""
    for member in obj.members:
        if member.value is value:
            return member
    raise ValueError("value is not the value of any member in the given object")


def _parse_json_value(source: str) -> _JsonNode:
    """Parse ``source`` as a JSON value expression (any value type)."""
    stripped = source.strip()
    if not stripped:
        raise ValueError("JSON value source is empty")
    # wrap the value in an array so parse_json can consume it (parse_json expects a document)
    try:
        doc = parse_json(f"[{stripped}]")
    except ParseError as err:
        raise ValueError(f"JSON value source did not parse: {err.detail}") from err
    if not isinstance(doc.root, _JsonArray) or len(doc.root.items) != 1:
        raise ValueError("JSON value source must be exactly one value")
    return doc.root.items[0]


def _parse_json_member(source: str) -> _JsonMember:
    """Parse ``source`` as a ``"key": value`` fragment into a ``_JsonMember``."""
    stripped = source.strip()
    if not stripped:
        raise ValueError("JSON member source is empty")
    try:
        doc = parse_json("{" + stripped + "}")
    except ParseError as err:
        raise ValueError(f"JSON member source did not parse: {err.detail}") from err
    if not isinstance(doc.root, _JsonObject) or len(doc.root.members) != 1:
        raise ValueError("JSON member source must be exactly one key/value pair")
    return doc.root.members[0]


def _document_with_replacement(
    tree: _JsonDocument,
    old_node: _JsonNode,
    new_node: _JsonNode,
) -> _JsonDocument:
    """Return a new document with ``old_node`` (by identity) replaced by ``new_node``.

    The result is re-serialized and re-parsed so the returned handle is a
    freshly validated ``_JsonDocument``.
    """
    new_tree, found = _replace_in_tree(tree, old_node, new_node)
    if not found:
        raise ValueError("old node was not found in the tree by identity")
    if not isinstance(new_tree, _JsonDocument):
        raise RuntimeError("tree replacement produced a non-document root")
    # re-serialize and re-parse so the result matches the parse-then-serialize invariant
    return parse_json(serialize_json(new_tree))


def _first_value_pre_ws(node: _JsonNode) -> str:
    """Return the leading whitespace of ``node``'s first token."""
    if isinstance(node, _JsonString | _JsonNumber | _JsonBool | _JsonNull):
        return node.tok.pre_ws
    if isinstance(node, _JsonMember):
        return node.key.tok.pre_ws
    if isinstance(node, _JsonObject | _JsonArray):
        return node.open.pre_ws
    raise TypeError(f"unknown JSON node type: {type(node).__name__}")


def _set_first_value_pre_ws(node: _JsonNode, ws: str) -> None:
    """Mutate ``node`` in place to set its first token's ``pre_ws``."""
    if isinstance(node, _JsonString | _JsonNumber | _JsonBool | _JsonNull):
        node.tok.pre_ws = ws
        return
    if isinstance(node, _JsonMember):
        node.key.tok.pre_ws = ws
        return
    if isinstance(node, _JsonObject | _JsonArray):
        node.open.pre_ws = ws
        return
    raise TypeError(f"unknown JSON node type: {type(node).__name__}")


def _detect_leading_ws(existing_pre_ws: list[str]) -> tuple[str, bool]:
    """Infer the standard per-member leading whitespace for a container.

    Returns ``(leading, multiline)`` where ``leading`` is the pre-token
    whitespace to apply uniformly, and ``multiline`` is whether the container
    spans multiple lines.
    """
    best = ""
    for ws in existing_pre_ws:
        if len(ws) > len(best):
            best = ws
    multiline = "\n" in best
    return best, multiline


def _renormalize_object_whitespace(obj: _JsonObject) -> _JsonObject:
    """Rewrite ``obj``'s whitespace so every member / comma formats consistently.

    ``close.pre_ws`` and ``open.pre_ws`` are preserved verbatim — only the
    per-member leading whitespace, commas, and (for empty objects) close
    whitespace are rewritten.
    """
    pre_ws_observed = [m.key.tok.pre_ws for m in obj.members]
    leading, multiline = _detect_leading_ws(pre_ws_observed)

    if not multiline:
        leading = leading if leading else " "

    new_members: list[_JsonMember] = []
    for i, m in enumerate(obj.members):
        # multiline: every member gets the full leading (newline + indent).
        # inline with existing members: only non-first members get leading (a space).
        ws = leading if (multiline or i > 0) else ""
        new_key = _JsonString(tok=_Tok(pre_ws=ws, text=m.key.tok.text))
        new_member = _JsonMember(key=new_key, colon=m.colon, value=m.value)
        new_members.append(new_member)

    commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(new_members) - 1))]
    # preserve close.pre_ws verbatim: it encodes the outer dedent we cannot re-derive
    # from members alone (we don't know the outer indent step from inside the container)
    return _JsonObject(
        open=_Tok(pre_ws=obj.open.pre_ws, text=obj.open.text),
        members=new_members,
        commas=commas,
        close=_Tok(pre_ws=obj.close.pre_ws, text=obj.close.text),
    )


def _renormalize_array_whitespace(arr: _JsonArray) -> _JsonArray:
    """Mirror of :func:`_renormalize_object_whitespace` for arrays."""
    pre_ws_observed = [_first_value_pre_ws(item) for item in arr.items]
    leading, multiline = _detect_leading_ws(pre_ws_observed)

    if not multiline:
        leading = leading if leading else " "

    new_items: list[_JsonNode] = []
    for i, item in enumerate(arr.items):
        ws = leading if (multiline or i > 0) else ""
        new_item = copy.deepcopy(item)
        _set_first_value_pre_ws(new_item, ws)
        new_items.append(new_item)

    commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(new_items) - 1))]
    return _JsonArray(
        open=_Tok(pre_ws=arr.open.pre_ws, text=arr.open.text),
        items=new_items,
        commas=commas,
        close=_Tok(pre_ws=arr.close.pre_ws, text=arr.close.text),
    )


# --------------------------------------------------------------------------- #
# StructuralLanguage implementation (stage 3)
# --------------------------------------------------------------------------- #


_JSON_VALUE_TYPES: tuple[type, ...] = (_JsonObject, _JsonArray, _JsonString, _JsonNumber, _JsonBool, _JsonNull)


class JsonStructuralLanguage(StructuralLanguage):
    """Structural backend for JSON documents.

    Parses with the hand-rolled round-trip CST from this module. Mutations
    build a fresh CST (sharing unchanged subtrees) and then re-parse the
    serialized source, so every returned handle is a freshly validated
    :class:`_JsonDocument` with the same round-trip guarantees as direct
    :func:`parse_json` output.
    """

    def __init__(self, name_resolver: LogicalNameResolver | None = None):
        """:param name_resolver: the resolver exposed via :attr:`name_resolver`.
        Defaults to a :class:`JsonLogicalNameResolver` rooted at the current
        working directory, mirroring the markdown backend's default.
        """
        # one resolver per backend instance; callers may inject their own to change root / extensions
        self._name_resolver = name_resolver or JsonLogicalNameResolver(Path.cwd())

    # ---- identity ----------------------------------------------------------

    @property
    def language_key(self) -> str:
        return "json"

    @property
    def kind_schema(self) -> KindSchema:
        return _JSON_KIND_SCHEMA

    @property
    def name_resolver(self) -> LogicalNameResolver:
        return self._name_resolver

    # ---- parse / serialize -------------------------------------------------

    def parse(self, source: str) -> _JsonDocument:
        # parse_json already raises ParseError with a source preview; nothing to add
        return parse_json(source)

    def serialize(self, tree: Any) -> str:
        if not isinstance(tree, _JsonNode):
            raise TypeError(f"cannot serialize handle of type {type(tree).__name__}")
        return serialize_json(tree)

    def render_node_source(self, node: Any) -> str:
        # every JSON CST node (incl. _JsonMember) serializes to its exact source
        # slice via _emit; .strip() drops a member's leading pre-token whitespace
        try:
            return serialize_json(node).strip()
        except TypeError:
            return super().render_node_source(node)

    # ---- symbol-tree introspection ----------------------------------------

    def root_kind(self, tree: Any) -> KindName:
        if not isinstance(tree, _JsonDocument):
            raise TypeError(f"root_kind expects a _JsonDocument, got {type(tree).__name__}")
        return "document"

    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, _JsonNode]]:
        # delegate to the module-level walk_symbols so stage-2 behaviour is shared with the class
        return walk_symbols(tree)

    # ---- declaration -------------------------------------------------------

    def build_declaration(
        self,
        kind: KindName,
        attributes: Mapping[str, Any],
        children: Iterable[Any],
    ) -> _JsonNode:
        self.kind_schema.get(kind)  # validates the kind name exists
        children_list = list(children)

        if kind == "document":
            raise DeclarationError(kind, "construct document via empty_source() plus insert_child()")

        if kind == "string":
            if children_list:
                raise DeclarationError(kind, "string takes no children")
            raw_text = _require_str(attributes, "raw_text", kind)
            _validate_string_literal(raw_text, kind)
            return _JsonString(tok=_Tok(pre_ws="", text=raw_text))

        if kind == "number":
            if children_list:
                raise DeclarationError(kind, "number takes no children")
            raw_text = _require_str(attributes, "raw_text", kind)
            _validate_number_literal(raw_text, kind)
            return _JsonNumber(tok=_Tok(pre_ws="", text=raw_text))

        if kind == "boolean":
            if children_list:
                raise DeclarationError(kind, "boolean takes no children")
            value = _require_bool(attributes, "value", kind)
            return _JsonBool(tok=_Tok(pre_ws="", text="true" if value else "false"))

        if kind == "null":
            if children_list:
                raise DeclarationError(kind, "null takes no children")
            return _JsonNull(tok=_Tok(pre_ws="", text="null"))

        if kind == "member":
            key = _require_str(attributes, "key", kind)
            if len(children_list) != 1:
                raise DeclarationError(kind, f"member requires exactly one value child, got {len(children_list)}")
            value_child = children_list[0]
            if not isinstance(value_child, _JSON_VALUE_TYPES):
                raise DeclarationError(kind, f"member child must be a JSON value, got {type(value_child).__name__}")
            # copy so downstream mutation cannot disturb the caller's handle
            value_copy: _JsonNode = copy.deepcopy(value_child)  # type: ignore[assignment]
            _clear_leading_ws(value_copy)
            key_node = _JsonString(tok=_Tok(pre_ws="", text=_stdlib_json.dumps(key)))
            return _JsonMember(key=key_node, colon=_Tok(pre_ws="", text=":"), value=value_copy)

        if kind == "object":
            for idx, child in enumerate(children_list):
                if not isinstance(child, _JsonMember):
                    raise DeclarationError(kind, f"object child {idx} must be a member, got {type(child).__name__}")
            members = [copy.deepcopy(c) for c in children_list]
            for m in members:
                _clear_leading_ws(m)
            commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(members) - 1))]
            return _JsonObject(
                open=_Tok(pre_ws="", text="{"),
                members=members,
                commas=commas,
                close=_Tok(pre_ws="", text="}"),
            )

        if kind == "array":
            for idx, child in enumerate(children_list):
                if not isinstance(child, _JSON_VALUE_TYPES):
                    raise DeclarationError(kind, f"array child {idx} must be a JSON value, got {type(child).__name__}")
            items = [copy.deepcopy(c) for c in children_list]
            for item in items:
                _clear_leading_ws(item)
            commas = [_Tok(pre_ws="", text=",") for _ in range(max(0, len(items) - 1))]
            return _JsonArray(
                open=_Tok(pre_ws="", text="["),
                items=items,
                commas=commas,
                close=_Tok(pre_ws="", text="]"),
            )

        raise DeclarationError(kind, f"build_declaration not supported for kind {kind!r}")

    # ---- insert / remove ---------------------------------------------------

    def insert_child(
        self,
        parent: Any,
        child: Any,
        anchor: Any | None = None,
        position: str = "end",
    ) -> _JsonDocument:
        if not isinstance(parent, _JsonDocument):
            raise TypeError(f"parent must be _JsonDocument, got {type(parent).__name__}")
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")
        if position in {"before", "after"} and anchor is None:
            raise ValueError(f"position {position!r} requires an anchor")

        root = parent.root
        new_root: _JsonNode

        if isinstance(root, _JsonObject):
            if not isinstance(child, _JsonMember):
                raise TypeError(f"inserting into object requires a _JsonMember, got {type(child).__name__}")
            member_copy: _JsonMember = copy.deepcopy(child)
            new_root = _object_with_inserted(root, member_copy, anchor, position)
            return self.parse(serialize_json(_JsonDocument(root=new_root, trailing_ws=parent.trailing_ws)))

        if isinstance(root, _JsonArray):
            if not isinstance(child, _JSON_VALUE_TYPES):
                raise TypeError(f"inserting into array requires a JSON value, got {type(child).__name__}")
            value_copy: _JsonNode = copy.deepcopy(child)  # type: ignore[assignment]
            new_root = _array_with_inserted(root, value_copy, anchor, position)
            return self.parse(serialize_json(_JsonDocument(root=new_root, trailing_ws=parent.trailing_ws)))

        raise ValueError(f"cannot insert into scalar root of kind {_value_kind(root)!r}")

    def remove_child(self, parent: Any, child: Any) -> _JsonDocument:
        if not isinstance(parent, _JsonDocument):
            raise TypeError(f"parent must be _JsonDocument, got {type(parent).__name__}")
        root = parent.root
        new_root: _JsonNode
        if isinstance(root, _JsonObject):
            new_root = _object_without(root, child)
            return self.parse(serialize_json(_JsonDocument(root=new_root, trailing_ws=parent.trailing_ws)))
        if isinstance(root, _JsonArray):
            new_root = _array_without(root, child)
            return self.parse(serialize_json(_JsonDocument(root=new_root, trailing_ws=parent.trailing_ws)))
        raise ValueError(f"cannot remove from scalar root of kind {_value_kind(root)!r}")

    # ---- container-member editing -----------------------------------------

    def container_insert_member(
        self,
        tree: Any,
        anchor_or_container_path: str,
        source: str,
        position: str = "end",
    ) -> _JsonDocument:
        if not isinstance(tree, _JsonDocument):
            raise TypeError(f"tree must be _JsonDocument, got {type(tree).__name__}")
        if position not in {"before", "after", "start", "end"}:
            raise ValueError(f"invalid position: {position!r}")

        if position in {"start", "end"}:
            container = _resolve_container(tree, anchor_or_container_path)
            anchor_node: _JsonNode | None = None
        else:
            # the anchor's parent is the container; the anchor itself is the positioning reference
            parent_path, _last = _split_member_path_json(anchor_or_container_path)
            container = _resolve_container(tree, parent_path) if parent_path else _root_as_container(tree)
            # for object anchors, walk_symbols yields the member's value — but identity-based
            # insertion needs the _JsonMember wrapper, so translate it when the container is an object
            target_value = _resolve_node_by_path(tree, anchor_or_container_path)
            if isinstance(container, _JsonObject):
                anchor_node = _find_member_for_value(container, target_value)
            else:
                anchor_node = target_value

        if isinstance(container, _JsonObject):
            child = _parse_json_member(source)
            inserted = _object_with_inserted(container, child, anchor_node, position)
            new_container: _JsonNode = _renormalize_object_whitespace(inserted)
        else:
            child_value = _parse_json_value(source)
            inserted_arr = _array_with_inserted(container, child_value, anchor_node, position)
            new_container = _renormalize_array_whitespace(inserted_arr)
        return _document_with_replacement(tree, container, new_container)

    def container_remove_member(self, tree: Any, member_path: str) -> _JsonDocument:
        if not isinstance(tree, _JsonDocument):
            raise TypeError(f"tree must be _JsonDocument, got {type(tree).__name__}")
        parent_path, _last = _split_member_path_json(member_path)
        container = _resolve_container(tree, parent_path) if parent_path else _root_as_container(tree)
        target = _resolve_node_by_path(tree, member_path)
        new_container: _JsonNode
        if isinstance(container, _JsonObject):
            member = _find_member_for_value(container, target)
            removed = _object_without(container, member)
            new_container = _renormalize_object_whitespace(removed)
        else:
            removed_arr = _array_without(container, target)
            new_container = _renormalize_array_whitespace(removed_arr)
        return _document_with_replacement(tree, container, new_container)

    def container_replace_member(self, tree: Any, member_path: str, source: str) -> _JsonDocument:
        if not isinstance(tree, _JsonDocument):
            raise TypeError(f"tree must be _JsonDocument, got {type(tree).__name__}")
        parent_path, _last = _split_member_path_json(member_path)
        container = _resolve_container(tree, parent_path) if parent_path else _root_as_container(tree)
        target = _resolve_node_by_path(tree, member_path)
        new_value = _parse_json_value(source)
        # keep the slot's original leading whitespace so serialization stays stable
        _set_first_value_pre_ws(new_value, _first_value_pre_ws(target))
        new_container: _JsonNode
        if isinstance(container, _JsonObject):
            member = _find_member_for_value(container, target)
            new_member = _JsonMember(key=member.key, colon=member.colon, value=new_value)
            new_members = [new_member if m is member else m for m in container.members]
            new_container = _JsonObject(
                open=container.open,
                members=new_members,
                commas=list(container.commas),
                close=container.close,
            )
        else:
            new_items = [new_value if item is target else item for item in container.items]
            new_container = _JsonArray(
                open=container.open,
                items=new_items,
                commas=list(container.commas),
                close=container.close,
            )
        return _document_with_replacement(tree, container, new_container)

    # ---- pattern matching & rewriting --------------------------------------

    def compile_pattern(self, pattern_source: str) -> AstPattern:
        if not pattern_source.strip():
            raise PatternError("parse", "pattern source is empty")
        try:
            tree = parse_json(pattern_source)
        except ParseError as err:
            raise PatternError("parse", f"pattern source is not valid JSON: {err.detail}") from err
        return _JsonPattern(source=pattern_source, tree=tree)

    def find_matches(
        self,
        tree: Any,
        pattern: AstPattern,
        scope: Any | None = None,
    ) -> Iterable[PatternMatch]:
        if not isinstance(tree, _JsonDocument):
            raise TypeError(f"tree must be a _JsonDocument, got {type(tree).__name__}")
        if not isinstance(pattern, _JsonPattern):
            raise TypeError(f"pattern must come from this backend's compile_pattern, got {type(pattern).__name__}")
        # scope narrows the search to a subtree; None means the whole document root
        root: _JsonNode = tree.root
        if scope is not None:
            if not isinstance(scope, _JsonNode):
                raise TypeError(f"scope must be a _JsonNode, got {type(scope).__name__}")
            root = scope
        matches: list[PatternMatch] = []
        pattern_root = pattern.tree.root
        for candidate in _iter_value_nodes(root):
            bindings: dict[str, _JsonNode] = {}
            if _match_value(pattern_root, candidate, bindings):
                matches.append(PatternMatch(node=candidate, bindings=dict(bindings), symbol_path=None))
        return matches

    def render_replacement(
        self,
        replacement_source: str,
        bindings: Mapping[str, Any],
    ) -> _JsonReplacement:
        if not replacement_source.strip():
            raise PatternError("parse", "replacement source is empty")
        try:
            tree = parse_json(replacement_source)
        except ParseError as err:
            raise PatternError("parse", f"replacement source is not valid JSON: {err.detail}") from err
        typed_bindings: dict[str, _JsonNode] = {}
        for name, value in bindings.items():
            if not isinstance(value, _JsonNode):
                raise PatternError("parse", f"binding {name!r} is not a JSON node; got {type(value).__name__}")
            typed_bindings[name] = value
        resolved = _render_capture_resolution(tree.root, typed_bindings)
        return _JsonReplacement(node=resolved)

    def apply_replacement(
        self,
        tree: Any,
        match: PatternMatch,
        replacement: Any,
    ) -> _JsonDocument:
        if not isinstance(tree, _JsonDocument):
            raise TypeError(f"tree must be a _JsonDocument, got {type(tree).__name__}")
        if not isinstance(replacement, _JsonReplacement):
            raise TypeError(f"replacement must come from this backend's render_replacement, got {type(replacement).__name__}")
        if not isinstance(match.node, _JsonNode):
            raise TypeError(f"match.node must be a _JsonNode, got {type(match.node).__name__}")
        new_tree, found = _replace_in_tree(tree, match.node, replacement.node)
        if not found:
            raise ValueError("match.node was not found in tree")
        assert isinstance(new_tree, _JsonDocument)
        # re-parse so the returned handle is a clean CST with valid invariants
        return self.parse(serialize_json(new_tree))

    # ---- new-source construction ------------------------------------------

    def empty_source(self, source_kind: KindName) -> _JsonDocument:
        if source_kind != "document":
            raise DeclarationError(source_kind, f"json has no source kind {source_kind!r}")
        # seed with an empty object so insert_child can append members immediately
        return self.parse("{}")
