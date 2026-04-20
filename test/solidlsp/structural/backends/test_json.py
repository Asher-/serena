"""Tests for the JSON structural backend CST parser and serializer.

This file covers the first stage of the JSON backend: the parse/serialize
round-trip invariant. Higher-level :class:`StructuralLanguage` methods (kind
schema, walk, mutation, patterns) will be tested in subsequent files as they
are implemented.
"""

from __future__ import annotations

import pytest

from solidlsp.structural.backends.json import (
    _JsonArray,
    _JsonBool,
    _JsonDocument,
    _JsonNull,
    _JsonNumber,
    _JsonObject,
    _JsonString,
    parse_json,
    serialize_json,
)
from solidlsp.structural.errors import ParseError

# --------------------------------------------------------------------------- #
# Round-trip corpus
# --------------------------------------------------------------------------- #

ROUND_TRIP_CORPUS = [
    # scalars
    "true",
    "false",
    "null",
    "0",
    "-0",
    "42",
    "-42",
    "3.14",
    "-0.001",
    "1e10",
    "1E+10",
    "1.5e-3",
    '"hello"',
    '""',
    '"with \\"quotes\\" and \\n newlines"',
    '"unicode \\u00e9"',
    # arrays
    "[]",
    "[1]",
    "[1, 2, 3]",
    '[1,"two",true,null]',
    "[[1,2],[3,4]]",
    # objects
    "{}",
    '{"a":1}',
    '{"a":1,"b":2}',
    '{"nested":{"inner":"value"}}',
    # whitespace variations (the invariant must preserve each)
    " true ",
    "\n\ttrue\n",
    "[\n  1,\n  2\n]",
    '{\n  "a": 1,\n  "b": 2\n}',
    '  {  "x"  :  [ 1 , 2 ]  }  \n',
    # trailing whitespace/newline at EOF
    "42\n",
    '"x"\n\n',
]


@pytest.mark.parametrize("source", ROUND_TRIP_CORPUS)
def test_parse_serialize_round_trip(source: str) -> None:
    # round-trip invariant: serialize(parse(s)) == s for every corpus entry
    tree = parse_json(source)
    assert serialize_json(tree) == source


# --------------------------------------------------------------------------- #
# Parser structural checks
# --------------------------------------------------------------------------- #


def test_root_is_document_with_root_value() -> None:
    # the parser wraps the root value in a _JsonDocument carrying trailing ws
    tree = parse_json('{"a":1}\n')
    assert isinstance(tree, _JsonDocument)
    assert isinstance(tree.root, _JsonObject)
    assert tree.trailing_ws == "\n"


def test_object_members_preserve_order_and_count() -> None:
    # members land in source order; commas record the separators
    tree = parse_json('{"a":1,"b":2,"c":3}')
    assert isinstance(tree.root, _JsonObject)
    obj = tree.root
    assert len(obj.members) == 3
    assert len(obj.commas) == 2
    keys = [m.key.tok.text for m in obj.members]
    assert keys == ['"a"', '"b"', '"c"']


def test_array_items_preserve_order_and_count() -> None:
    # array items land in source order; commas record the separators
    tree = parse_json("[10, 20, 30, 40]")
    assert isinstance(tree.root, _JsonArray)
    arr = tree.root
    assert len(arr.items) == 4
    assert len(arr.commas) == 3


def test_numeric_literal_text_preserved() -> None:
    # the numeric node preserves exact source text, not a decoded value
    tree = parse_json("1.500e+03")
    assert isinstance(tree.root, _JsonNumber)
    assert tree.root.tok.text == "1.500e+03"


def test_string_literal_raw_text_preserved() -> None:
    # the string node preserves quotes and escape sequences verbatim
    tree = parse_json('"a\\tb"')
    assert isinstance(tree.root, _JsonString)
    assert tree.root.tok.text == '"a\\tb"'


def test_boolean_and_null_nodes() -> None:
    # true/false produce _JsonBool; null produces _JsonNull
    assert isinstance(parse_json("true").root, _JsonBool)
    assert isinstance(parse_json("false").root, _JsonBool)
    assert isinstance(parse_json("null").root, _JsonNull)


# --------------------------------------------------------------------------- #
# Parser error paths
# --------------------------------------------------------------------------- #


def test_unterminated_string_raises() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse_json('"hello')
    assert "unterminated string" in exc_info.value.detail


def test_unescaped_control_char_in_string_raises() -> None:
    # literal newline inside quotes is illegal; must be escaped as \n
    with pytest.raises(ParseError) as exc_info:
        parse_json('"a\nb"')
    assert "unescaped control character" in exc_info.value.detail


def test_trailing_comma_in_object_raises() -> None:
    # RFC 8259 forbids trailing commas; a comma before '}' becomes a missing-key error
    with pytest.raises(ParseError):
        parse_json('{"a":1,}')


def test_trailing_comma_in_array_raises() -> None:
    with pytest.raises(ParseError):
        parse_json("[1,2,]")


def test_missing_colon_in_object_raises() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse_json('{"a" 1}')
    assert "':'" in exc_info.value.detail


def test_bare_identifier_rejected() -> None:
    # only true/false/null keywords are accepted bare
    with pytest.raises(ParseError):
        parse_json("hello")


def test_trailing_content_after_root_raises() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse_json("42 43")
    assert "trailing content" in exc_info.value.detail


def test_empty_input_raises() -> None:
    with pytest.raises(ParseError):
        parse_json("")


def test_malformed_number_missing_fraction_digit_raises() -> None:
    with pytest.raises(ParseError):
        parse_json("1.")


def test_malformed_number_missing_exponent_digit_raises() -> None:
    with pytest.raises(ParseError):
        parse_json("1e")


# --------------------------------------------------------------------------- #
# Tricky whitespace cases
# --------------------------------------------------------------------------- #


def test_whitespace_only_between_tokens_round_trips() -> None:
    # mixed whitespace (spaces, tabs, CR, LF) between every token is preserved
    src = '{\n\t"x"\t: \r\n  [ 1 , 2 ]  }'
    tree = parse_json(src)
    assert serialize_json(tree) == src


def test_nested_structure_round_trips() -> None:
    # a deeper mix exercises every node type in combination
    src = """{
  "name": "demo",
  "tags": ["a", "b", "c"],
  "meta": {
    "count": 3,
    "enabled": true,
    "value": null
  }
}
"""
    tree = parse_json(src)
    assert serialize_json(tree) == src
