"""Tests for the Swift structural backend.

Mirrors the C++ backend's test shape: curated edge-case fixtures for
round-trip, plus focused coverage of walk_symbols, build_declaration,
insert_child, remove_child, and empty_source. Pattern matching over the
sigil grammar is deferred to a follow-up refinement and is covered only
by a smoke test that the ``find_matches`` op does not raise.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from solidlsp.structural.backends.swift import (
    SwiftStructuralLanguage,
    _SwiftTree,
    swift_kind_schema,
)
from solidlsp.structural.errors import DeclarationError, PatternError
from test.solidlsp.structural.harness import assert_round_trip

# -----------------------------------------------------------------------------
# Shared fixture
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Iterator[SwiftStructuralLanguage]:
    # module-scoped so the subprocess is spun up once per test module
    be = SwiftStructuralLanguage()
    try:
        yield be
    finally:
        be.close()


# -----------------------------------------------------------------------------
# Edge-case round-trip fixtures
# -----------------------------------------------------------------------------


_EDGE_CASES: tuple[tuple[str, str], ...] = (
    ("empty", ""),
    ("only-newline", "\n"),
    ("trailing-newline", "let x = 1\n"),
    ("no-final-newline", "let x = 1"),
    ("crlf-line-endings", "let x = 1\r\nlet y = 2\r\n"),
    ("mixed-line-endings", "let x = 1\nlet y = 2\r\n"),
    ("comments-only", "// a comment\n/* another */\n"),
    (
        "import-and-class",
        "import Foundation\n\nclass Foo {\n    var x: Int = 1\n}\n",
    ),
    (
        "struct-with-methods",
        "struct Point {\n    var x: Int\n    var y: Int\n    func squared() -> Int { x * x + y * y }\n}\n",
    ),
    (
        "generic-function",
        "func id<T>(_ value: T) -> T { value }\n",
    ),
    (
        "enum-with-cases",
        "enum Direction {\n    case north\n    case south\n    case east\n    case west\n}\n",
    ),
    (
        "protocol-and-extension",
        "protocol Greeter {\n    func greet() -> String\n}\n\nextension Greeter {\n    func greet() -> String { \"hi\" }\n}\n",
    ),
    (
        "actor-decl",
        "actor Counter {\n    private var value: Int = 0\n    func increment() { value += 1 }\n}\n",
    ),
    (
        "string-with-escapes",
        "let s = \"hello\\n\\t\\\"world\\\"\"\n",
    ),
    (
        "multiline-string",
        "let s = \"\"\"\n  line1\n  line2\n  \"\"\"\n",
    ),
    (
        "raw-string",
        "let r = #\"C:\\path\\to\\file\"#\n",
    ),
    (
        "async-throws",
        "func fetch() async throws -> Data { fatalError() }\n",
    ),
    (
        "closure-expr",
        "let f = { (x: Int) -> Int in x + 1 }\n",
    ),
    (
        "property-wrapper",
        "@propertyWrapper struct Clamped {\n    var wrappedValue: Int\n}\n",
    ),
    (
        "availability-attribute",
        "@available(macOS 12.0, *)\nfunc modern() {}\n",
    ),
    (
        "nested-type",
        "struct Outer {\n    struct Inner {\n        let value: Int\n    }\n}\n",
    ),
)


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


class TestRoundTripFixtures:
    @pytest.mark.parametrize(("label", "source"), _EDGE_CASES, ids=[case[0] for case in _EDGE_CASES])
    def test_edge_case_round_trips(
        self,
        backend: SwiftStructuralLanguage,
        label: str,
        source: str,
    ) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_language_key_and_source_kinds(self, backend: SwiftStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "swift"
        assert schema.source_kinds == frozenset({"source_file"})

    def test_expected_kinds_present(self, backend: SwiftStructuralLanguage) -> None:
        schema = backend.kind_schema
        expected = {
            "source_file", "import", "class", "struct", "enum", "protocol",
            "extension", "actor", "function", "method", "initializer",
            "variable", "property", "type_alias", "enum_case",
        }
        assert expected <= set(schema.kinds)

    def test_methods_restricted_to_type_bodies(self) -> None:
        schema = swift_kind_schema()
        method = schema.get("method")
        assert method.allowed_parent_kinds == frozenset(
            {"class", "struct", "enum", "protocol", "extension", "actor"}
        )

    def test_import_restricted_to_source_file(self) -> None:
        schema = swift_kind_schema()
        imp = schema.get("import")
        assert imp.allowed_parent_kinds == frozenset({"source_file"})

    def test_validate_composition_rules(self) -> None:
        schema = swift_kind_schema()
        # method at source root is rejected
        with pytest.raises(DeclarationError):
            schema.validate_composition("source_file", "method")
        # method inside class is fine
        schema.validate_composition("class", "method")
        # enum_case only under enum
        with pytest.raises(DeclarationError):
            schema.validate_composition("class", "enum_case")
        schema.validate_composition("enum", "enum_case")


# -----------------------------------------------------------------------------
# Walk symbols
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_symbols(self, backend: SwiftStructuralLanguage) -> None:
        source = (
            "import Foundation\n"
            "class Foo {\n"
            "    var x: Int = 1\n"
            "    func bar() {}\n"
            "}\n"
        )
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        kinds = {name_path: kind for name_path, kind, _ref in symbols}
        assert kinds["Foundation"] == "import"
        assert kinds["Foo"] == "class"
        assert kinds["Foo/x"] == "property"
        assert kinds["Foo/bar"] == "method"

    def test_walk_symbol_carries_body_range_for_compound(
        self,
        backend: SwiftStructuralLanguage,
    ) -> None:
        source = "class Foo {\n    var x: Int = 1\n}\n"
        tree = backend.parse(source)
        refs = {
            name_path: ref
            for name_path, _kind, ref in backend.walk_symbols(tree)
        }
        foo = refs["Foo"]
        assert foo.body_range is not None
        start, end = foo.body_range
        assert source[start:end].strip().startswith("var x")

    def test_walk_detects_method_vs_function(self, backend: SwiftStructuralLanguage) -> None:
        source = (
            "func topLevel() {}\n"
            "struct S {\n"
            "    func member() {}\n"
            "}\n"
        )
        tree = backend.parse(source)
        kinds = {
            name_path: kind
            for name_path, kind, _ref in backend.walk_symbols(tree)
        }
        assert kinds["topLevel"] == "function"
        assert kinds["S/member"] == "method"


# -----------------------------------------------------------------------------
# Root kind
# -----------------------------------------------------------------------------


class TestRootKind:
    def test_root_kind_is_source_file(self, backend: SwiftStructuralLanguage) -> None:
        tree = backend.parse("")
        assert backend.root_kind(tree) == "source_file"


# -----------------------------------------------------------------------------
# Declaration and mutation
# -----------------------------------------------------------------------------


class TestDeclarationAndMutation:
    def test_build_import(self, backend: SwiftStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "import",
            {"statement": "import Foundation"},
            [],
        )
        assert decl.source == "import Foundation\n"

    def test_build_function(self, backend: SwiftStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "function",
            {
                "name": "greet",
                "parameters": "_ name: String",
                "return_type": "String",
                "body": '    "hello, \\(name)"\n',
            },
            [],
        )
        assert "func greet(_ name: String) -> String" in decl.source
        assert decl.source.endswith("}\n")

    def test_build_class_with_method_child(self, backend: SwiftStructuralLanguage) -> None:
        method = backend.build_declaration(
            "method",
            {"name": "hello", "parameters": "", "body": "    print(\"hi\")\n"},
            [],
        )
        cls = backend.build_declaration(
            "class",
            {"name": "Greeter"},
            [method],
        )
        assert "class Greeter" in cls.source
        assert "func hello()" in cls.source

    def test_build_declaration_missing_required_raises(
        self,
        backend: SwiftStructuralLanguage,
    ) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {}, [])

    def test_insert_child_at_end_of_source(
        self,
        backend: SwiftStructuralLanguage,
    ) -> None:
        tree = backend.parse("import Foundation\n")
        decl = backend.build_declaration(
            "variable",
            {"name": "answer", "type": "Int", "initializer": "42", "is_let": True},
            [],
        )
        new_tree = backend.insert_child(tree, decl, anchor=None, position="end")
        assert backend.serialize(new_tree) == "import Foundation\nlet answer: Int = 42\n"

    def test_insert_child_before_anchor(
        self,
        backend: SwiftStructuralLanguage,
    ) -> None:
        tree = backend.parse("let a = 1\nlet c = 3\n")
        symbols = {np: ref for np, _k, ref in backend.walk_symbols(tree)}
        decl = backend.build_declaration(
            "variable",
            {"name": "b", "initializer": "2", "is_let": True},
            [],
        )
        new_tree = backend.insert_child(tree, decl, anchor=symbols["c"], position="before")
        serialized = backend.serialize(new_tree)
        assert serialized == "let a = 1\nlet b = 2\nlet c = 3\n"

    def test_remove_child(self, backend: SwiftStructuralLanguage) -> None:
        source = "let a = 1\nlet b = 2\nlet c = 3\n"
        tree = backend.parse(source)
        symbols = {np: ref for np, _k, ref in backend.walk_symbols(tree)}
        new_tree = backend.remove_child(tree, symbols["b"])
        serialized = backend.serialize(new_tree)
        assert "let b = 2" not in serialized
        assert "let a = 1" in serialized
        assert "let c = 3" in serialized


# -----------------------------------------------------------------------------
# Patterns (smoke only — deferred refinement)
# -----------------------------------------------------------------------------


class TestPatternMatching:
    def test_empty_pattern_rejected(self, backend: SwiftStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_compile_returns_usable_pattern(self, backend: SwiftStructuralLanguage) -> None:
        pattern = backend.compile_pattern("let $name = $value\n")
        assert pattern is not None

    def test_find_matches_does_not_raise(self, backend: SwiftStructuralLanguage) -> None:
        tree = backend.parse("let a = 1\nlet b = 2\n")
        pattern = backend.compile_pattern("let $name = $value\n")
        # list() forces the generator; result contents are not asserted (deferred)
        list(backend.find_matches(tree, pattern, scope=None))


# -----------------------------------------------------------------------------
# Empty source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: SwiftStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        assert backend.serialize(tree) == ""
        assert isinstance(tree, _SwiftTree)

    def test_empty_source_rejects_unknown_kind(
        self,
        backend: SwiftStructuralLanguage,
    ) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("class")
