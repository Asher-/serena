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
    JsonStructuralLanguage,
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


# --------------------------------------------------------------------------- #
# StructuralLanguage surface (stage 3)
# --------------------------------------------------------------------------- #


from solidlsp.structural.backends.json import (
    JsonStructuralLanguage,
    _JsonPattern,
    _JsonReplacement,
)
from solidlsp.structural.errors import PatternError
from solidlsp.structural.patterns import PatternMatch


@pytest.fixture()
def backend(tmp_path: Path) -> JsonStructuralLanguage:
    # one resolver per backend rooted at tmp_path so file lookups stay hermetic
    return JsonStructuralLanguage(JsonLogicalNameResolver(tmp_path))


class TestStructuralLanguageIdentity:
    """Identity surface: language_key, kind_schema, name_resolver."""

    def test_language_key_returns_json(self, backend: JsonStructuralLanguage) -> None:
        assert backend.language_key == "json"

    def test_kind_schema_returns_json_schema(self, backend: JsonStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "json"
        assert schema.source_kinds == frozenset({"document"})

    def test_name_resolver_is_json_resolver(self, backend: JsonStructuralLanguage) -> None:
        assert isinstance(backend.name_resolver, JsonLogicalNameResolver)


class TestStructuralParseSerialize:
    """parse/serialize surface on the class delegate to the module helpers."""

    def test_parse_and_serialize_round_trip(self, backend: JsonStructuralLanguage) -> None:
        source = '{"a": 1, "b": [true, null]}\n'
        tree = backend.parse(source)
        assert backend.serialize(tree) == source

    def test_parse_malformed_raises_parse_error(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(ParseError):
            backend.parse("{not json}")

    def test_serialize_rejects_non_cst_handle(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.serialize("not a cst node")


class TestStructuralRootKind:
    """root_kind returns ``document`` for JSON documents; anything else errors."""

    def test_root_kind_is_document(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("[]")
        assert backend.root_kind(tree) == "document"

    def test_root_kind_rejects_non_document(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.root_kind("not a tree")


class TestStructuralWalkSymbols:
    """walk_symbols on the class delegates to the module-level function."""

    def test_walks_object_members(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a": 1, "b": 2}')
        paths = [p for p, _, _ in backend.walk_symbols(tree)]
        assert paths == ["a", "b"]

    def test_walks_nested_compound_paths(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"outer": {"inner": [1, 2]}}')
        paths = {p for p, _, _ in backend.walk_symbols(tree)}
        assert "outer" in paths
        assert "outer/inner" in paths
        assert "outer/inner/[0]" in paths
        assert "outer/inner/[1]" in paths

    def test_walks_empty_object_yields_nothing(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("{}")
        assert list(backend.walk_symbols(tree)) == []


class TestBuildDeclaration:
    """build_declaration produces opaque CST nodes ready for insert/replace."""

    def test_string_declaration(self, backend: JsonStructuralLanguage) -> None:
        node = backend.build_declaration("string", {"raw_text": '"hello"'}, [])
        assert backend.serialize(node) == '"hello"'

    def test_string_with_escapes_preserved_verbatim(self, backend: JsonStructuralLanguage) -> None:
        node = backend.build_declaration("string", {"raw_text": r'"a\nb"'}, [])
        assert backend.serialize(node) == r'"a\nb"'

    def test_string_raw_text_without_quotes_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("string", {"raw_text": "hello"}, [])

    def test_string_missing_raw_text_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("string", {}, [])

    def test_number_declaration(self, backend: JsonStructuralLanguage) -> None:
        node = backend.build_declaration("number", {"raw_text": "3.14"}, [])
        assert backend.serialize(node) == "3.14"

    def test_number_malformed_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("number", {"raw_text": "3.14.15"}, [])

    def test_boolean_true_declaration(self, backend: JsonStructuralLanguage) -> None:
        node = backend.build_declaration("boolean", {"value": True}, [])
        assert backend.serialize(node) == "true"

    def test_boolean_false_declaration(self, backend: JsonStructuralLanguage) -> None:
        node = backend.build_declaration("boolean", {"value": False}, [])
        assert backend.serialize(node) == "false"

    def test_boolean_missing_value_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("boolean", {}, [])

    def test_null_declaration(self, backend: JsonStructuralLanguage) -> None:
        node = backend.build_declaration("null", {}, [])
        assert backend.serialize(node) == "null"

    def test_member_wraps_value_child(self, backend: JsonStructuralLanguage) -> None:
        value = backend.build_declaration("number", {"raw_text": "1"}, [])
        member = backend.build_declaration("member", {"key": "foo"}, [value])
        assert backend.serialize(member) == '"foo":1'

    def test_member_key_with_special_chars_is_quoted(self, backend: JsonStructuralLanguage) -> None:
        # the member key is escaped on render, not taken verbatim
        value = backend.build_declaration("null", {}, [])
        member = backend.build_declaration("member", {"key": 'a "b"'}, [value])
        assert backend.serialize(member) == '"a \\"b\\"":null'

    def test_member_requires_single_child(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("member", {"key": "k"}, [])

    def test_object_from_members(self, backend: JsonStructuralLanguage) -> None:
        v1 = backend.build_declaration("number", {"raw_text": "1"}, [])
        v2 = backend.build_declaration("number", {"raw_text": "2"}, [])
        m1 = backend.build_declaration("member", {"key": "a"}, [v1])
        m2 = backend.build_declaration("member", {"key": "b"}, [v2])
        obj = backend.build_declaration("object", {}, [m1, m2])
        assert backend.serialize(obj) == '{"a":1,"b":2}'

    def test_object_empty(self, backend: JsonStructuralLanguage) -> None:
        obj = backend.build_declaration("object", {}, [])
        assert backend.serialize(obj) == "{}"

    def test_object_rejects_non_member_children(self, backend: JsonStructuralLanguage) -> None:
        v = backend.build_declaration("number", {"raw_text": "1"}, [])
        with pytest.raises(DeclarationError):
            backend.build_declaration("object", {}, [v])

    def test_array_from_items(self, backend: JsonStructuralLanguage) -> None:
        items = [backend.build_declaration("number", {"raw_text": str(i)}, []) for i in range(3)]
        arr = backend.build_declaration("array", {}, items)
        assert backend.serialize(arr) == "[0,1,2]"

    def test_array_empty(self, backend: JsonStructuralLanguage) -> None:
        arr = backend.build_declaration("array", {}, [])
        assert backend.serialize(arr) == "[]"

    def test_array_rejects_member_children(self, backend: JsonStructuralLanguage) -> None:
        m = backend.build_declaration("member", {"key": "k"}, [backend.build_declaration("null", {}, [])])
        with pytest.raises(DeclarationError):
            backend.build_declaration("array", {}, [m])

    def test_document_kind_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("document", {}, [])

    def test_unknown_kind_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(KeyError):
            backend.build_declaration("mystery", {}, [])


class TestInsertChild:
    """insert_child appends/prepends/positions members in object or array roots."""

    def test_insert_member_into_empty_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.empty_source("document")
        value = backend.build_declaration("number", {"raw_text": "1"}, [])
        member = backend.build_declaration("member", {"key": "a"}, [value])
        new_tree = backend.insert_child(tree, member)
        assert backend.serialize(new_tree) == '{"a":1}'

    def test_insert_member_at_end_of_populated_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1}')
        value = backend.build_declaration("number", {"raw_text": "2"}, [])
        member = backend.build_declaration("member", {"key": "b"}, [value])
        new_tree = backend.insert_child(tree, member)
        assert backend.serialize(new_tree) == '{"a":1,"b":2}'

    def test_insert_member_at_start_of_populated_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1}')
        value = backend.build_declaration("number", {"raw_text": "0"}, [])
        member = backend.build_declaration("member", {"key": "z"}, [value])
        new_tree = backend.insert_child(tree, member, position="start")
        assert backend.serialize(new_tree) == '{"z":0,"a":1}'

    def test_insert_member_before_anchor(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1,"c":3}')
        assert isinstance(tree.root, _JsonObject)
        anchor = tree.root.members[1]  # the "c" member
        value = backend.build_declaration("number", {"raw_text": "2"}, [])
        member = backend.build_declaration("member", {"key": "b"}, [value])
        new_tree = backend.insert_child(tree, member, anchor=anchor, position="before")
        assert backend.serialize(new_tree) == '{"a":1,"b":2,"c":3}'

    def test_insert_member_after_anchor(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1,"c":3}')
        assert isinstance(tree.root, _JsonObject)
        anchor = tree.root.members[0]  # the "a" member
        value = backend.build_declaration("number", {"raw_text": "2"}, [])
        member = backend.build_declaration("member", {"key": "b"}, [value])
        new_tree = backend.insert_child(tree, member, anchor=anchor, position="after")
        assert backend.serialize(new_tree) == '{"a":1,"b":2,"c":3}'

    def test_insert_value_into_array(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("[1,2]")
        value = backend.build_declaration("number", {"raw_text": "3"}, [])
        new_tree = backend.insert_child(tree, value)
        assert backend.serialize(new_tree) == "[1,2,3]"

    def test_insert_rejects_value_into_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("{}")
        value = backend.build_declaration("number", {"raw_text": "1"}, [])
        with pytest.raises(TypeError):
            backend.insert_child(tree, value)

    def test_insert_rejects_member_into_array(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("[]")
        value = backend.build_declaration("number", {"raw_text": "1"}, [])
        member = backend.build_declaration("member", {"key": "a"}, [value])
        with pytest.raises(TypeError):
            backend.insert_child(tree, member)

    def test_insert_rejects_scalar_root(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("42")
        value = backend.build_declaration("number", {"raw_text": "1"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, value)

    def test_insert_before_without_anchor_rejected(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1}')
        value = backend.build_declaration("number", {"raw_text": "2"}, [])
        member = backend.build_declaration("member", {"key": "b"}, [value])
        with pytest.raises(ValueError):
            backend.insert_child(tree, member, position="before")

    def test_insert_invalid_position_rejected(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("{}")
        value = backend.build_declaration("number", {"raw_text": "1"}, [])
        member = backend.build_declaration("member", {"key": "a"}, [value])
        with pytest.raises(ValueError):
            backend.insert_child(tree, member, position="middle")

    def test_old_tree_unchanged_after_insert(self, backend: JsonStructuralLanguage) -> None:
        # handles are values: inserting produces a new tree and leaves the old one intact
        tree = backend.parse('{"a":1}')
        value = backend.build_declaration("number", {"raw_text": "2"}, [])
        member = backend.build_declaration("member", {"key": "b"}, [value])
        backend.insert_child(tree, member)
        assert backend.serialize(tree) == '{"a":1}'


class TestRemoveChild:
    """remove_child removes a member or array item by identity."""

    def test_remove_member_from_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1,"b":2}')
        assert isinstance(tree.root, _JsonObject)
        victim = tree.root.members[0]
        new_tree = backend.remove_child(tree, victim)
        assert backend.serialize(new_tree) == '{"b":2}'

    def test_remove_member_middle(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1,"b":2,"c":3}')
        assert isinstance(tree.root, _JsonObject)
        victim = tree.root.members[1]
        new_tree = backend.remove_child(tree, victim)
        assert backend.serialize(new_tree) == '{"a":1,"c":3}'

    def test_remove_leaves_empty_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1}')
        assert isinstance(tree.root, _JsonObject)
        victim = tree.root.members[0]
        new_tree = backend.remove_child(tree, victim)
        assert backend.serialize(new_tree) == "{}"

    def test_remove_item_from_array(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("[1,2,3]")
        assert isinstance(tree.root, _JsonArray)
        victim = tree.root.items[1]
        new_tree = backend.remove_child(tree, victim)
        assert backend.serialize(new_tree) == "[1,3]"

    def test_remove_unknown_child_raises(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1}')
        other_tree = backend.parse('{"x":0}')
        assert isinstance(other_tree.root, _JsonObject)
        stranger = other_tree.root.members[0]
        with pytest.raises(ValueError):
            backend.remove_child(tree, stranger)

    def test_remove_from_scalar_root_rejected(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("42")
        with pytest.raises(ValueError):
            backend.remove_child(tree, object())

    def test_old_tree_unchanged_after_remove(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1,"b":2}')
        assert isinstance(tree.root, _JsonObject)
        victim = tree.root.members[0]
        backend.remove_child(tree, victim)
        assert backend.serialize(tree) == '{"a":1,"b":2}'


class TestEmptySource:
    """empty_source yields a parsable, round-trip-valid seed document."""

    def test_document_empty_source(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.empty_source("document")
        assert isinstance(tree, _JsonDocument)
        assert backend.serialize(tree) == "{}"

    def test_unknown_source_kind_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("module")


class TestCompilePattern:
    """compile_pattern accepts valid JSON patterns and rejects empty input."""

    def test_empty_pattern_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("   ")

    def test_malformed_pattern_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("{not json")

    def test_valid_pattern_returns_astpattern(self, backend: JsonStructuralLanguage) -> None:
        pattern = backend.compile_pattern("42")
        assert isinstance(pattern, _JsonPattern)


class TestFindMatches:
    """find_matches walks the tree and returns matches with identity-preserved nodes."""

    def test_exact_literal_match(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("[1, 2, 3]")
        pattern = backend.compile_pattern("2")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1

    def test_no_match_yields_empty(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("[1, 2, 3]")
        pattern = backend.compile_pattern("99")
        assert list(backend.find_matches(tree, pattern)) == []

    def test_wildcard_matches_every_value(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1}')
        pattern = backend.compile_pattern('"$_"')
        matches = list(backend.find_matches(tree, pattern))
        # matches the whole document root object, its member's value, plus the matched string key appears? No; keys are not values.
        # expect: root object + inner number 1 => 2 matches
        assert len(matches) == 2

    def test_named_capture_binds(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"user":{"name":"alice"}}')
        pattern = backend.compile_pattern('{"name":"$n"}')
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1
        match = matches[0]
        assert "n" in match.bindings
        bound = match.bindings["n"]
        assert isinstance(bound, _JsonString)
        assert backend.serialize(bound).strip() == '"alice"'

    def test_object_member_count_mismatch_no_match(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1,"b":2}')
        pattern = backend.compile_pattern('{"a":1}')
        assert list(backend.find_matches(tree, pattern)) == []

    def test_array_length_mismatch_no_match(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("[[1, 2, 3]]")
        pattern = backend.compile_pattern("[1, 2]")
        assert list(backend.find_matches(tree, pattern)) == []

    def test_scope_restricts_search(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":42,"b":{"c":42}}')
        pattern = backend.compile_pattern("42")
        # full search hits both 42s
        assert len(list(backend.find_matches(tree, pattern))) == 2
        # scoped to the nested object hits only one
        assert isinstance(tree.root, _JsonObject)
        scope = tree.root.members[1].value  # the {"c":42} object
        assert len(list(backend.find_matches(tree, pattern, scope=scope))) == 1

    def test_find_rejects_foreign_pattern(self, backend: JsonStructuralLanguage) -> None:
        class _Alien:
            pass

        tree = backend.parse("{}")
        with pytest.raises(TypeError):
            backend.find_matches(tree, _Alien())  # type: ignore[arg-type]


class TestRenderReplacement:
    """render_replacement substitutes captures into a replacement template."""

    def test_substitutes_captured_node(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"v":1}')
        pattern = backend.compile_pattern('{"v":"$x"}')
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement('{"v":"$x","copy":"$x"}', match.bindings)
        assert isinstance(replacement, _JsonReplacement)
        assert backend.serialize(replacement.node) == '{"v":1,"copy":1}'

    def test_missing_binding_raises(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement('{"v":"$x"}', {})

    def test_wildcard_in_replacement_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement('{"v":"$_"}', {})

    def test_empty_replacement_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("   ", {})

    def test_malformed_replacement_rejected(self, backend: JsonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("{not json", {})


class TestApplyReplacement:
    """apply_replacement splices a rendered subtree back into the tree."""

    def test_replaces_matched_node(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"items":[1,2,3]}')
        pattern = backend.compile_pattern("2")
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement("99", {})
        new_tree = backend.apply_replacement(tree, match, replacement)
        assert backend.serialize(new_tree) == '{"items":[1,99,3]}'

    def test_replaces_object_subtree(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"outer":{"inner":1}}')
        pattern = backend.compile_pattern('{"inner":"$x"}')
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement('{"new":"$x"}', match.bindings)
        new_tree = backend.apply_replacement(tree, match, replacement)
        assert backend.serialize(new_tree) == '{"outer":{"new":1}}'

    def test_replace_root_returns_fresh_document(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("42")
        pattern = backend.compile_pattern("42")
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement("99", {})
        new_tree = backend.apply_replacement(tree, match, replacement)
        assert backend.serialize(new_tree) == "99"

    def test_apply_rejects_foreign_replacement(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse("42")
        pattern = backend.compile_pattern("42")
        match = next(iter(backend.find_matches(tree, pattern)))
        with pytest.raises(TypeError):
            backend.apply_replacement(tree, match, "raw string")  # type: ignore[arg-type]

    def test_apply_rejects_unknown_match_node(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a":1}')
        other = backend.parse('{"x":0}')
        replacement = backend.render_replacement("99", {})
        fake_match = PatternMatch(node=other.root, bindings={}, symbol_path=None)
        with pytest.raises(ValueError):
            backend.apply_replacement(tree, fake_match, replacement)


# --------------------------------------------------------------------------- #
# Container-member editing
# --------------------------------------------------------------------------- #


class TestJsonContainerMemberEdits:
    """container_insert_member / container_remove_member / container_replace_member
    preserve formatting for multi-line and nested containers.
    """

    _MULTI = '{\n  "a": 1,\n  "nested": {\n    "x": 10,\n    "y": 20\n  },\n  "list": [\n    1,\n    2\n  ]\n}\n'

    def test_insert_at_end_of_root_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_insert_member(tree, "", '"c": 3', position="end")
        serialized = backend.serialize(new_tree)
        assert '"c": 3' in serialized
        # original shape preserved: newlines and 2-space indent
        assert '  "c": 3\n}' in serialized

    def test_insert_into_nested_object(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_insert_member(tree, "nested", '"z": 30', position="end")
        serialized = backend.serialize(new_tree)
        assert '    "z": 30\n  }' in serialized

    def test_insert_before_nested_member(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_insert_member(tree, "nested/y", '"m": 100', position="before")
        serialized = backend.serialize(new_tree)
        # m comes before y with matching indent
        m_line = '    "m": 100'
        y_line = '    "y": 20'
        assert serialized.index(m_line) < serialized.index(y_line)

    def test_insert_into_nested_array(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_insert_member(tree, "list", "99", position="end")
        serialized = backend.serialize(new_tree)
        assert "    99\n  ]" in serialized

    def test_replace_nested_object_value(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_replace_member(tree, "nested/y", "999")
        serialized = backend.serialize(new_tree)
        assert '"y": 999' in serialized
        # the original x is untouched
        assert '"x": 10' in serialized

    def test_replace_nested_array_item(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_replace_member(tree, "list/[0]", "99")
        serialized = backend.serialize(new_tree)
        assert "    99,\n    2" in serialized

    def test_remove_nested_object_member(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_remove_member(tree, "nested/x")
        serialized = backend.serialize(new_tree)
        assert '"x"' not in serialized
        assert '"y": 20' in serialized
        # the nested close-brace still has 2-space indent
        assert '    "y": 20\n  }' in serialized

    def test_remove_nested_array_item(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        new_tree = backend.container_remove_member(tree, "list/[0]")
        serialized = backend.serialize(new_tree)
        assert "    2\n  ]" in serialized

    def test_insert_rejects_malformed_member(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        with pytest.raises(ValueError):
            backend.container_insert_member(tree, "", "not a key:value", position="end")

    def test_insert_rejects_missing_anchor(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        with pytest.raises(ValueError):
            backend.container_insert_member(tree, "nested/missing", '"m": 1', position="before")

    def test_remove_rejects_missing_member(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse(self._MULTI)
        with pytest.raises(ValueError):
            backend.container_remove_member(tree, "nested/missing")


class TestRenderNodeSource:
    """A walked object member renders its exact source slice (spec-v2 §5.2)."""

    def test_object_member_renders_exact_slice(self, backend: JsonStructuralLanguage) -> None:
        tree = backend.parse('{"a": 1, "b": 2}')
        nodes = {path: node for path, _kind, node in walk_symbols(tree)}
        assert backend.render_node_source(nodes["a"]) == '"a": 1'
        assert backend.render_node_source(nodes["b"]) == '"b": 2'
