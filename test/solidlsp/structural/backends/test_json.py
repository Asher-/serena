"""Tests for the JSON structural backend.

Covers:

* Stage 1: parse/serialize round-trip invariant and CST shape.
* Stage 2: kind schema, logical name resolution, and ``walk_symbols``.

Higher-level :class:`StructuralLanguage` methods (mutation, patterns, registry
wiring) will be tested in subsequent stages as they are implemented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from solidlsp.structural.backends.json import (
    JsonLogicalNameResolver,
    _JsonArray,
    _JsonBool,
    _JsonDocument,
    _JsonMember,
    _JsonNull,
    _JsonNumber,
    _JsonObject,
    _JsonString,
    json_kind_schema,
    parse_json,
    serialize_json,
    walk_symbols,
)
from solidlsp.structural.errors import DeclarationError, NameResolutionError, ParseError

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


# --------------------------------------------------------------------------- #
# Kind schema (stage 2)
# --------------------------------------------------------------------------- #


class TestKindSchema:
    """Exercises the kind vocabulary returned by :func:`json_kind_schema`."""

    def test_language_key_is_json(self) -> None:
        assert json_kind_schema().language_key == "json"

    def test_document_is_the_only_source_kind(self) -> None:
        assert json_kind_schema().source_kinds == frozenset({"document"})

    def test_every_expected_kind_is_present(self) -> None:
        # every JSON structural vocabulary member must appear
        expected = {"document", "object", "array", "member", "string", "number", "boolean", "null"}
        assert set(json_kind_schema().kinds) == expected

    def test_member_only_allowed_under_object(self) -> None:
        schema = json_kind_schema()
        schema.validate_composition("object", "member")
        with pytest.raises(DeclarationError):
            schema.validate_composition("array", "member")
        with pytest.raises(DeclarationError):
            schema.validate_composition("document", "member")

    def test_objects_and_arrays_valid_under_value_parents(self) -> None:
        schema = json_kind_schema()
        # objects and arrays are values, so they may appear under document / array / member
        for parent in ("document", "array", "member"):
            schema.validate_composition(parent, "object")
            schema.validate_composition(parent, "array")

    def test_scalars_are_leaves(self) -> None:
        # leaf kinds do not allow any children
        schema = json_kind_schema()
        for leaf in ("string", "number", "boolean", "null"):
            assert schema.get(leaf).allowed_child_kinds == frozenset()

    def test_document_only_accepts_values(self) -> None:
        # a document never directly contains a member
        schema = json_kind_schema()
        with pytest.raises(DeclarationError):
            schema.validate_composition("document", "member")

    def test_member_requires_key_attribute(self) -> None:
        # the member kind is the only one carrying a required ``key`` attribute
        member = json_kind_schema().get("member")
        key_attrs = [a for a in member.attributes if a.name == "key"]
        assert len(key_attrs) == 1
        assert key_attrs[0].required is True


# --------------------------------------------------------------------------- #
# Logical name resolution (stage 2)
# --------------------------------------------------------------------------- #


class TestLogicalNameResolver:
    """Exercises :class:`JsonLogicalNameResolver`'s parse and resolve."""

    def test_parse_slash_form(self, tmp_path: Path) -> None:
        # slash-delimited names split into per-segment parts
        resolver = JsonLogicalNameResolver(tmp_path)
        name = resolver.parse("config/defaults")
        assert name.parts == ("config", "defaults")
        assert name.raw == "config/defaults"

    def test_parse_dotted_form(self, tmp_path: Path) -> None:
        # dot-delimited names split into per-segment parts identically
        resolver = JsonLogicalNameResolver(tmp_path)
        name = resolver.parse("config.defaults")
        assert name.parts == ("config", "defaults")

    def test_parse_rejects_empty(self, tmp_path: Path) -> None:
        # empty names have no parts to resolve
        resolver = JsonLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("")

    def test_parse_rejects_invalid_parts(self, tmp_path: Path) -> None:
        # invalid segments (empty, starting with hyphen, whitespace) are rejected
        resolver = JsonLogicalNameResolver(tmp_path)
        for raw in ("config//defaults", "-leading-dash", "has space/foo", "foo/"):
            with pytest.raises(NameResolutionError):
                resolver.parse(raw)

    def test_resolve_existing_file_reports_exists_true(self, tmp_path: Path) -> None:
        # an existing ``.json`` under the project root resolves with exists=True
        (tmp_path / "config.json").write_text("{}\n")
        resolver = JsonLogicalNameResolver(tmp_path)
        resolution = resolver.resolve(resolver.parse("config"))
        assert resolution.exists is True
        assert resolution.relative_path == "config.json"
        assert resolution.source_kind == "document"

    def test_resolve_nonexistent_synthesizes_creation_path(self, tmp_path: Path) -> None:
        # missing files still resolve to a creation path with exists=False
        resolver = JsonLogicalNameResolver(tmp_path)
        resolution = resolver.resolve(resolver.parse("packages/defaults"))
        assert resolution.exists is False
        assert resolution.relative_path == "packages/defaults.json"
        assert resolution.source_kind == "document"


# --------------------------------------------------------------------------- #
# Symbol walking (stage 2)
# --------------------------------------------------------------------------- #


class TestWalkSymbols:
    """Exercises :func:`walk_symbols` over documents, objects, and arrays."""

    def test_root_scalar_yields_nothing(self) -> None:
        # a scalar root has no addressable symbols of its own
        assert list(walk_symbols(parse_json('"hello"'))) == []
        assert list(walk_symbols(parse_json("42"))) == []
        assert list(walk_symbols(parse_json("true"))) == []
        assert list(walk_symbols(parse_json("null"))) == []

    def test_root_object_yields_each_member(self) -> None:
        # each member emits a ``member``-kind symbol under its key
        tree = parse_json('{"foo": 1, "bar": 2}')
        paths = [(p, k) for p, k, _ in walk_symbols(tree)]
        assert paths == [("foo", "member"), ("bar", "member")]

    def test_root_array_yields_indexed_elements(self) -> None:
        # array indices wear bracketed segments, kinds reflect each value's type
        tree = parse_json('[true, "x", 3]')
        paths = [(p, k) for p, k, _ in walk_symbols(tree)]
        assert paths == [("[0]", "boolean"), ("[1]", "string"), ("[2]", "number")]

    def test_nested_object_under_member_is_walked(self) -> None:
        # nested members appear under ``parent/child`` paths
        tree = parse_json('{"outer": {"inner": 1}}')
        by_path = {p: k for p, k, _ in walk_symbols(tree)}
        assert by_path == {"outer": "member", "outer/inner": "member"}

    def test_array_under_member_propagates_bracket_paths(self) -> None:
        # array elements inside a member's value keep the member's prefix
        tree = parse_json('{"items": [10, 20]}')
        by_path = {p: k for p, k, _ in walk_symbols(tree)}
        assert by_path == {
            "items": "member",
            "items/[0]": "number",
            "items/[1]": "number",
        }

    def test_object_under_array_gets_member_children(self) -> None:
        # objects within arrays yield both the indexed element and its members
        tree = parse_json('[{"a": 1}, {"b": 2}]')
        by_path = {p: k for p, k, _ in walk_symbols(tree)}
        assert by_path == {
            "[0]": "object",
            "[0]/a": "member",
            "[1]": "object",
            "[1]/b": "member",
        }

    def test_walk_symbols_yields_member_nodes_for_members(self) -> None:
        # member entries carry the :class:`_JsonMember` node, not the value
        tree = parse_json('{"foo": 1}')
        entries = list(walk_symbols(tree))
        assert len(entries) == 1
        path, kind, node = entries[0]
        assert path == "foo"
        assert kind == "member"
        assert isinstance(node, _JsonMember)

    def test_walk_symbols_rejects_non_document(self) -> None:
        # callers must pass a :class:`_JsonDocument`, not a raw node
        with pytest.raises(TypeError):
            walk_symbols(_JsonString.__new__(_JsonString))  # type: ignore[arg-type]

    def test_key_with_escape_is_decoded(self) -> None:
        # keys emerge in their semantic (unescaped) form, matching JSON decoding
        tree = parse_json(r'{"a\nb": 1}')
        path, _, _ = next(iter(walk_symbols(tree)))
        assert path == "a\nb"
