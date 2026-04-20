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

This file implements the first stage of the JSON backend: parse + serialize +
round-trip invariant. Higher-level :class:`StructuralLanguage` surface
(kind schema, name resolution, walk, mutation, patterns) will be layered on
in subsequent commits.
"""

from __future__ import annotations

import json as _stdlib_json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from solidlsp.structural.errors import NameResolutionError, ParseError
from solidlsp.structural.kinds import AttributeSpec, KindName, KindSchema, KindSpec
from solidlsp.structural.names import LogicalName, LogicalNameResolver, NameResolution

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
