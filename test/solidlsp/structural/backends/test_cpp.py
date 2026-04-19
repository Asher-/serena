"""Tests for :mod:`solidlsp.structural.backends.cpp`.

Organized into suites mirroring the Python backend's layout:

* **round-trip fixtures** — curated edge-case C++ sources.
* **kind schema** — shape of the published kind vocabulary.
* **name resolver** — logical namespace path → header/source file.
* **symbol walk** — cursor-tree traversal yields the right structural paths.
* **declaration + mutation** — build_declaration + insert_child + remove_child.
* **pattern matching and rewriting** — ``$``-sigil grammar on cursor trees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from solidlsp.structural.backends.cpp import (
    CppLogicalNameResolver,
    CppStructuralLanguage,
    _CppSymbolRef,
    cpp_kind_schema,
)
from solidlsp.structural.errors import (
    DeclarationError,
    NameResolutionError,
    ParseError,
    PatternError,
)
from test.solidlsp.structural.harness import assert_round_trip


# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def backend() -> CppStructuralLanguage:
    return CppStructuralLanguage()


_EDGE_CASES: tuple[tuple[str, str], ...] = (
    ("empty", ""),
    ("only-newline", "\n"),
    ("trailing-newline", "int x = 1;\n"),
    ("no-final-newline", "int x = 1;"),
    ("crlf-line-endings", "int x = 1;\r\nint y = 2;\r\n"),
    ("mixed-line-endings", "int x = 1;\nint y = 2;\r\n"),
    ("comments-only", "// a comment\n/* another */\n"),
    (
        "preprocessor",
        "#include <vector>\n#define FOO 1\n#if FOO\nint x;\n#endif\n",
    ),
    (
        "class-with-template",
        "template <typename T>\nclass Box {\npublic:\n    T value;\n};\n",
    ),
    (
        "namespace-nested",
        "namespace a { namespace b {\n    int x = 1;\n}}\n",
    ),
    (
        "lambda-in-body",
        "auto f = [](int x) { return x + 1; };\n",
    ),
    (
        "requires-clause-cpp20",
        "template <typename T>\nrequires (sizeof(T) > 0)\nT id(T x) { return x; }\n",
    ),
    (
        "raw-string-literal",
        "const char* s = R\"(line1\nline2)\";\n",
    ),
    (
        "string-with-escapes",
        "const char* s = \"hello\\n\\t\\\"world\\\"\";\n",
    ),
    (
        "operator-overload",
        "struct P { int x; P operator+(P o) const { return {x + o.x}; } };\n",
    ),
)


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


class TestRoundTripFixtures:
    @pytest.mark.parametrize(("label", "source"), _EDGE_CASES, ids=[case[0] for case in _EDGE_CASES])
    def test_edge_case_round_trips(self, backend: CppStructuralLanguage, label: str, source: str) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Parse errors
# -----------------------------------------------------------------------------


class TestParseErrors:
    def test_truly_broken_source_is_tolerated_by_libclang(self, backend: CppStructuralLanguage) -> None:
        # libclang is highly forgiving; it produces a partial AST for most broken sources.
        # Fatal errors (out of memory, crashes) are rare; most syntax errors are 'errors', not 'fatal'.
        # We verify the backend does not raise for broken-but-parseable sources.
        tree = backend.parse("int f( {\n")
        assert backend.serialize(tree) == "int f( {\n"


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_language_key_and_source_kinds(self, backend: CppStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "cpp"
        assert schema.source_kinds == frozenset({"translation_unit"})

    def test_expected_kinds_present(self, backend: CppStructuralLanguage) -> None:
        schema = backend.kind_schema
        expected = {
            "translation_unit",
            "include",
            "namespace",
            "class",
            "struct",
            "union",
            "function",
            "method",
            "field",
            "variable",
            "type_alias",
            "enum",
            "enum_constant",
        }
        assert expected <= set(schema.kinds)

    def test_methods_only_allowed_under_records(self) -> None:
        schema = cpp_kind_schema()
        method = schema.get("method")
        assert method.allowed_parent_kinds == frozenset({"class", "struct", "union"})

    def test_include_only_allowed_at_translation_unit(self) -> None:
        schema = cpp_kind_schema()
        include = schema.get("include")
        assert include.allowed_parent_kinds == frozenset({"translation_unit"})

    def test_validate_composition_rules(self) -> None:
        schema = cpp_kind_schema()
        # method directly at TU is rejected
        with pytest.raises(DeclarationError):
            schema.validate_composition("translation_unit", "method")
        # method inside class is fine
        schema.validate_composition("class", "method")
        # nested namespaces are fine
        schema.validate_composition("namespace", "namespace")
        # include inside namespace is rejected
        with pytest.raises(DeclarationError):
            schema.validate_composition("namespace", "include")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_colon_colon_form(self, tmp_path: Path) -> None:
        resolver = CppLogicalNameResolver(tmp_path)
        name = resolver.parse("foo::bar::baz")
        assert name.parts == ("foo", "bar", "baz")

    def test_parse_dotted_form(self, tmp_path: Path) -> None:
        resolver = CppLogicalNameResolver(tmp_path)
        name = resolver.parse("foo.bar.baz")
        assert name.parts == ("foo", "bar", "baz")

    @pytest.mark.parametrize("invalid", ["", "foo::", "::foo", "a..b", "1a"])
    def test_parse_rejects_invalid(self, tmp_path: Path, invalid: str) -> None:
        resolver = CppLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse(invalid)

    def test_resolve_existing_hpp(self, tmp_path: Path) -> None:
        src_root = tmp_path / "include"
        (src_root / "foo").mkdir(parents=True)
        (src_root / "foo" / "bar.hpp").write_text("")
        resolver = CppLogicalNameResolver(tmp_path, source_roots=[src_root])
        resolution = resolver.resolve(resolver.parse("foo::bar"))
        assert resolution.exists is True
        assert resolution.relative_path == "include/foo/bar.hpp"
        assert resolution.source_kind == "translation_unit"

    def test_resolve_prefers_header_over_source(self, tmp_path: Path) -> None:
        src_root = tmp_path / "src"
        (src_root / "foo").mkdir(parents=True)
        (src_root / "foo" / "bar.hpp").write_text("")
        (src_root / "foo" / "bar.cpp").write_text("")
        resolver = CppLogicalNameResolver(tmp_path, source_roots=[src_root])
        resolution = resolver.resolve(resolver.parse("foo::bar"))
        assert resolution.relative_path == "src/foo/bar.hpp"

    def test_resolve_nonexistent_synthesizes(self, tmp_path: Path) -> None:
        src_root = tmp_path / "include"
        src_root.mkdir()
        resolver = CppLogicalNameResolver(tmp_path, source_roots=[src_root])
        resolution = resolver.resolve(resolver.parse("new::module"))
        assert resolution.exists is False
        assert resolution.relative_path == "include/new/module.hpp"


# -----------------------------------------------------------------------------
# Symbol walk
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_symbols(self, backend: CppStructuralLanguage) -> None:
        source = (
            "#include <vector>\n"
            "\n"
            "namespace foo {\n"
            "\n"
            "int g = 7;\n"
            "\n"
            "int greet(int x) { return x + 1; }\n"
            "\n"
            "class Bar {\n"
            "public:\n"
            "    int field;\n"
            "    int val() const { return 0; }\n"
            "};\n"
            "\n"
            "}\n"
        )
        tree = backend.parse(source)
        symbols = [(path, kind) for path, kind, _ in backend.walk_symbols(tree)]
        assert ("foo", "namespace") in symbols
        assert ("foo/g", "variable") in symbols
        assert ("foo/greet", "function") in symbols
        assert ("foo/Bar", "class") in symbols
        assert ("foo/Bar/field", "field") in symbols
        assert ("foo/Bar/val", "method") in symbols

    def test_walk_symbol_carries_body_range_for_compound(self, backend: CppStructuralLanguage) -> None:
        source = "namespace foo { int x = 1; }\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        foo_ref = next(ref for path, _kind, ref in symbols if path == "foo")
        assert foo_ref.body_range is not None
        start, end = foo_ref.body_range
        inside = source[start:end]
        # the body range lies strictly between the braces (exclusive of { and })
        assert "int x = 1;" in inside
        assert "{" not in inside
        assert "}" not in inside

    def test_root_kind_is_translation_unit(self, backend: CppStructuralLanguage) -> None:
        tree = backend.parse("")
        assert backend.root_kind(tree) == "translation_unit"


# -----------------------------------------------------------------------------
# Declaration + mutation
# -----------------------------------------------------------------------------


class TestDeclarationAndMutation:
    def test_build_include(self, backend: CppStructuralLanguage) -> None:
        decl = backend.build_declaration("include", {"statement": "#include <string>"}, ())
        assert decl.source == "#include <string>\n"

    def test_build_function(self, backend: CppStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "function",
            {
                "name": "add",
                "parameters": "int a, int b",
                "return_type": "int",
                "body": "return a + b;",
            },
            (),
        )
        assert "int add(int a, int b)" in decl.source
        assert "return a + b;" in decl.source

    def test_build_class_with_method_child(self, backend: CppStructuralLanguage) -> None:
        method = backend.build_declaration(
            "method",
            {"name": "val", "parameters": "", "return_type": "int", "body": "return 0;"},
            (),
        )
        cls = backend.build_declaration(
            "class",
            {"name": "Bar", "body": "public:\n"},
            (method,),
        )
        assert "class Bar" in cls.source
        assert "public:" in cls.source
        assert "int val()" in cls.source

    def test_build_declaration_missing_required_raises(self, backend: CppStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("function", {"name": "foo"}, ())  # missing return_type

    def test_insert_child_at_end_of_translation_unit(self, backend: CppStructuralLanguage) -> None:
        tree = backend.parse("int x = 1;\n")
        decl = backend.build_declaration("include", {"statement": "#include <string>"}, ())
        new_tree = backend.insert_child(tree, decl, anchor=None, position="end")
        assert "#include <string>" in backend.serialize(new_tree)
        # original tree unchanged
        assert "#include <string>" not in backend.serialize(tree)

    def test_insert_child_before_anchor(self, backend: CppStructuralLanguage) -> None:
        tree = backend.parse("int x = 1;\nint y = 2;\n")
        symbols = {path: ref for path, _kind, ref in backend.walk_symbols(tree)}
        y_ref = symbols["y"]
        decl = backend.build_declaration("include", {"statement": "#include <map>"}, ())
        new_tree = backend.insert_child(tree, decl, anchor=y_ref, position="before")
        serialized = backend.serialize(new_tree)
        assert serialized.index("#include <map>") > serialized.index("int x = 1;")
        assert serialized.index("#include <map>") < serialized.index("int y = 2;")

    def test_insert_child_into_namespace_body(self, backend: CppStructuralLanguage) -> None:
        tree = backend.parse("namespace foo {\nint x = 1;\n}\n")
        symbols = {path: ref for path, _kind, ref in backend.walk_symbols(tree)}
        foo_ref = symbols["foo"]
        decl = backend.build_declaration(
            "variable", {"name": "y", "type": "int", "initializer": "2"}, ()
        )
        new_tree = backend.insert_child(tree, decl, anchor=foo_ref, position="end")
        serialized = backend.serialize(new_tree)
        assert "int y = 2;" in serialized
        # the inserted variable lives inside the namespace body, not outside
        namespace_close = serialized.rindex("}")
        assert serialized.index("int y") < namespace_close

    def test_remove_child(self, backend: CppStructuralLanguage) -> None:
        tree = backend.parse("int x = 1;\nint y = 2;\n")
        symbols = {path: ref for path, _kind, ref in backend.walk_symbols(tree)}
        y_ref = symbols["y"]
        new_tree = backend.remove_child(tree, y_ref)
        assert "int y = 2;" not in backend.serialize(new_tree)
        assert "int x = 1;" in backend.serialize(new_tree)


# -----------------------------------------------------------------------------
# Pattern matching
# -----------------------------------------------------------------------------


class TestPatternMatching:
    def test_empty_pattern_rejected(self, backend: CppStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_compile_returns_usable_pattern(self, backend: CppStructuralLanguage) -> None:
        # just verifies compile doesn't raise for a reasonable fragment
        pattern = backend.compile_pattern("$x + $y")
        assert pattern.placeholders  # at least one placeholder was encoded

    def test_find_matches_does_not_raise(self, backend: CppStructuralLanguage) -> None:
        # the structural cursor match is best-effort in M2; we only verify that
        # find_matches runs to completion on a concrete tree + pattern combination
        tree = backend.parse("int f() { return 1 + 2; }\n")
        pattern = backend.compile_pattern("return $x;")
        # exercise the call path; we don't require matches to land on the right cursor in M2
        list(backend.find_matches(tree, pattern))


# -----------------------------------------------------------------------------
# empty_source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: CppStructuralLanguage) -> None:
        empty = backend.empty_source("translation_unit")
        assert backend.serialize(empty) == ""

    def test_empty_source_rejects_unknown_kind(self, backend: CppStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("class")
