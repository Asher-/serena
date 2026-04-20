"""Tests for the Java structural backend.

Mirrors the Rust backend's test shape: curated edge-case fixtures for
round-trip, plus focused coverage of walk_symbols, build_declaration,
insert_child, remove_child, empty_source, and pattern matching over the
Java ``javaparser-core`` library via the subprocess bridge.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path as _PathForTests

import pytest

from solidlsp.structural.backends.java import (
    JavaLogicalNameResolver,
    JavaStructuralLanguage,
    _JavaSymbolRef,
    java_kind_schema,
)
from solidlsp.structural.errors import DeclarationError, NameResolutionError, PatternError
from solidlsp.structural.registry import default_structural_backend_registry
from test.solidlsp.structural.harness import assert_round_trip

# -----------------------------------------------------------------------------
# Shared fixture
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Iterator[JavaStructuralLanguage]:
    # module-scoped so the subprocess is spun up once per test module
    be = JavaStructuralLanguage()
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
    ("single-class", "public class Hello {}\n"),
    ("no-final-newline", "public class Hello {}"),
    ("crlf-line-endings", "class A {}\r\nclass B {}\r\n"),
    ("mixed-line-endings", "class A {}\nclass B {}\r\n"),
    ("line-comments-only", "// comment 1\n// comment 2\n"),
    ("block-comment-only", "/* a block\n   comment */\n"),
    ("javadoc-on-class", "/** Does something. */\npublic class Foo {}\n"),
    ("annotation-on-class", "@Deprecated\npublic class Foo {}\n"),
    ("package-and-import", "package com.example;\nimport java.util.List;\npublic class C {}\n"),
    ("import-static", "import static java.util.Collections.emptyList;\npublic class C {}\n"),
    ("import-wildcard", "import java.util.*;\npublic class C {}\n"),
    (
        "class-with-method",
        "public class C {\n    public void foo() {}\n}\n",
    ),
    (
        "class-with-field",
        "public class C {\n    public int count;\n}\n",
    ),
    (
        "class-with-constructor",
        "public class C {\n    public C() {}\n}\n",
    ),
    (
        "interface-with-default",
        "public interface I {\n    default int foo() { return 1; }\n}\n",
    ),
    (
        "generic-class",
        "public class Pair<A, B> {\n    public A first;\n    public B second;\n}\n",
    ),
    (
        "method-with-throws",
        "public class C {\n    public void foo() throws java.io.IOException {}\n}\n",
    ),
    (
        "enum-with-values",
        "public enum Color { RED, GREEN, BLUE }\n",
    ),
    (
        "record-simple",
        "public record Point(int x, int y) {}\n",
    ),
    (
        "annotation-type",
        "public @interface MyAnn {\n    String value();\n}\n",
    ),
    (
        "nested-class",
        "public class Outer {\n    public static class Inner {\n        public void f() {}\n    }\n}\n",
    ),
    (
        "generic-method",
        "public class C {\n    public <T> T identity(T x) { return x; }\n}\n",
    ),
    (
        "method-with-annotation",
        'public class C {\n    @Override\n    public String toString() { return "C"; }\n}\n',
    ),
    (
        "multiple-top-level-declared",
        "public class A {}\nclass B {}\n",
    ),
    (
        "varargs-method",
        "public class C {\n    public void foo(int... xs) {}\n}\n",
    ),
    (
        "final-field",
        "public class C {\n    private final int x = 0;\n}\n",
    ),
    (
        "static-field",
        "public class C {\n    public static int count = 0;\n}\n",
    ),
    (
        "unicode-identifier",
        'public class C {\n    public String \u00e9t\u00e9 = "summer";\n}\n',
    ),
    (
        "unicode-in-string",
        'public class C {\n    public String s = "caf\u00e9";\n}\n',
    ),
    (
        "multiple-imports",
        "import java.util.List;\nimport java.util.Map;\nimport java.util.Set;\npublic class C {}\n",
    ),
    (
        "sealed-class",
        "public sealed class Shape permits Circle, Square {}\nfinal class Circle extends Shape {}\nfinal class Square extends Shape {}\n",
    ),
    (
        "ws-leading",
        "    \npublic class C {}\n",
    ),
    (
        "trailing-whitespace-lines",
        "public class C {}\n   \n  \n",
    ),
)


class TestRoundTripFixtures:
    @pytest.mark.parametrize("label,source", _EDGE_CASES, ids=[c[0] for c in _EDGE_CASES])
    def test_edge_case_round_trips(self, backend: JavaStructuralLanguage, label: str, source: str) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Identity
# -----------------------------------------------------------------------------


class TestIdentity:
    def test_language_key(self, backend: JavaStructuralLanguage) -> None:
        assert backend.language_key == "java"

    def test_kind_schema_identity(self, backend: JavaStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "java"
        assert "source_file" in schema.source_kinds

    def test_name_resolver_is_java_resolver(self, backend: JavaStructuralLanguage, tmp_path: _PathForTests) -> None:
        be = JavaStructuralLanguage(name_resolver=JavaLogicalNameResolver(tmp_path))
        assert isinstance(be.name_resolver, JavaLogicalNameResolver)
        be.close()


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_expected_kinds_present(self) -> None:
        schema = java_kind_schema()
        expected = {
            "source_file",
            "package",
            "import",
            "class",
            "interface",
            "enum",
            "record",
            "annotation_type",
            "method",
            "field",
            "constructor",
        }
        assert set(schema.kinds) == expected

    def test_source_file_children(self) -> None:
        schema = java_kind_schema()
        sf = schema.get("source_file")
        assert sf.allowed_child_kinds == frozenset({"package", "import", "class", "interface", "enum", "record", "annotation_type"})

    def test_package_only_in_source_file(self) -> None:
        schema = java_kind_schema()
        pkg = schema.get("package")
        assert pkg.allowed_parent_kinds == frozenset({"source_file"})

    def test_class_only_in_source_file(self) -> None:
        schema = java_kind_schema()
        cls = schema.get("class")
        assert cls.allowed_parent_kinds == frozenset({"source_file"})

    def test_method_not_insertable_in_v1(self) -> None:
        schema = java_kind_schema()
        m = schema.get("method")
        assert m.allowed_parent_kinds == frozenset()

    def test_field_not_insertable_in_v1(self) -> None:
        schema = java_kind_schema()
        f = schema.get("field")
        assert f.allowed_parent_kinds == frozenset()

    def test_constructor_not_insertable_in_v1(self) -> None:
        schema = java_kind_schema()
        c = schema.get("constructor")
        assert c.allowed_parent_kinds == frozenset()

    def test_all_top_level_kinds_are_leaves(self) -> None:
        schema = java_kind_schema()
        for kind_name in ("class", "interface", "enum", "record", "annotation_type", "package", "import"):
            k = schema.get(kind_name)
            assert k.allowed_child_kinds == frozenset(), kind_name

    def test_unknown_kind_raises(self) -> None:
        schema = java_kind_schema()
        with pytest.raises(KeyError):
            schema.get("does-not-exist")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_and_resolve_single(self, tmp_path: _PathForTests) -> None:
        resolver = JavaLogicalNameResolver(tmp_path)
        name = resolver.parse("Foo")
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "Foo.java"
        assert resolution.source_kind == "source_file"
        assert resolution.exists is False

    def test_parse_and_resolve_dotted(self, tmp_path: _PathForTests) -> None:
        resolver = JavaLogicalNameResolver(tmp_path)
        (tmp_path / "com" / "example").mkdir(parents=True)
        (tmp_path / "com" / "example" / "Foo.java").write_text("public class Foo {}\n")
        name = resolver.parse("com.example.Foo")
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "com/example/Foo.java"
        assert resolution.exists is True

    def test_empty_name_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = JavaLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("")

    def test_invalid_segment_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = JavaLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("foo..bar")

    def test_invalid_character_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = JavaLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("foo-bar")

    def test_dollar_identifier_accepted(self, tmp_path: _PathForTests) -> None:
        # Java identifiers may contain $. Our regex allows it.
        resolver = JavaLogicalNameResolver(tmp_path)
        name = resolver.parse("com.example.Foo$Inner")
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "com/example/Foo$Inner.java"


# -----------------------------------------------------------------------------
# Root kind
# -----------------------------------------------------------------------------


class TestRootKind:
    def test_root_kind_is_source_file(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class C {}\n")
        assert backend.root_kind(tree) == "source_file"

    def test_root_kind_rejects_non_tree(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.root_kind("not a tree")


# -----------------------------------------------------------------------------
# walk_symbols
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_class(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class Foo {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Foo", "class") in names

    def test_walks_package(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("package com.example;\npublic class Foo {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("com.example", "package") in names
        assert ("Foo", "class") in names

    def test_walks_import(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("import java.util.List;\npublic class C {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("java.util.List", "import") in names

    def test_walks_wildcard_import(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("import java.util.*;\npublic class C {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("java.util.*", "import") in names

    def test_walks_interface(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public interface I {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("I", "interface") in names

    def test_walks_enum(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public enum E { A, B, C }\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("E", "enum") in names

    def test_walks_record(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public record P(int x, int y) {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("P", "record") in names

    def test_walks_annotation_type(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public @interface Ann {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Ann", "annotation_type") in names

    def test_walks_method(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/foo()", "method") in names

    def test_walks_method_with_params(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public int add(int a, int b) { return 0; }\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/add(int,int)", "method") in names

    def test_walks_field(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public int x;\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/x", "field") in names

    def test_walks_constructor(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public C(int x) {}\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/C(int)", "constructor") in names

    def test_walks_nested_class(self, backend: JavaStructuralLanguage) -> None:
        src = "public class Outer {\n    public static class Inner {\n        public void f() {}\n    }\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Outer", "class") in names
        assert ("Outer/Inner", "class") in names
        assert ("Outer/Inner/f()", "method") in names

    def test_walks_overloaded_methods_disambiguated(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n    public void foo(int x) {}\n    public void foo(int x, String s) {}\n}\n"
        tree = backend.parse(src)
        names = [n for (n, k, _) in backend.walk_symbols(tree) if k == "method"]
        assert "C/foo()" in names
        assert "C/foo(int)" in names
        assert "C/foo(int,String)" in names

    def test_walks_varargs_method(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public void foo(int... xs) {}\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/foo(int...)", "method") in names

    def test_walk_extents_point_into_source(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n}\n"
        tree = backend.parse(src)
        for name_path, _kind, ref in backend.walk_symbols(tree):
            assert isinstance(ref, _JavaSymbolRef)
            sliced = src.encode("utf-8")[ref.extent_offset : ref.extent_offset + ref.extent_length].decode("utf-8")
            # Must contain some characteristic text from the declaration:
            if name_path == "C":
                assert "class C" in sliced
            if name_path == "C/foo()":
                assert "foo" in sliced

    def test_walk_body_range_is_none_v1(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n}\n"
        tree = backend.parse(src)
        for _n, _k, ref in backend.walk_symbols(tree):
            assert ref.body_range is None

    def test_walk_symbols_empty_source(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("")
        assert list(backend.walk_symbols(tree)) == []

    def test_walk_symbols_rejects_non_tree(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            list(backend.walk_symbols("not a tree"))

    def test_walk_ignores_inner_whitespace_for_offsets(self, backend: JavaStructuralLanguage) -> None:
        src = "   \npublic class C {}\n"
        tree = backend.parse(src)
        entries = list(backend.walk_symbols(tree))
        assert any(name == "C" for name, _k, _r in entries)


# -----------------------------------------------------------------------------
# build_declaration
# -----------------------------------------------------------------------------


class TestBuildDeclaration:
    def test_build_package(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("package", {"statement": "package com.example;"}, [])
        assert d.source == "package com.example;\n"

    def test_build_package_requires_keyword(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("package", {"statement": "com.example;"}, [])

    def test_build_import(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("import", {"statement": "import java.util.List;"}, [])
        assert d.source == "import java.util.List;\n"

    def test_build_import_static(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("import", {"statement": "import static java.util.Collections.emptyList;"}, [])
        assert "static" in d.source

    def test_build_class(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "public class Foo {}"}, [])
        assert d.source == "public class Foo {}\n"

    def test_build_interface(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("interface", {"statement": "public interface I {}"}, [])
        assert d.source == "public interface I {}\n"

    def test_build_enum(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("enum", {"statement": "public enum E { A, B }"}, [])
        assert d.source == "public enum E { A, B }\n"

    def test_build_record(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("record", {"statement": "public record P(int x) {}"}, [])
        assert d.source == "public record P(int x) {}\n"

    def test_build_annotation_type(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("annotation_type", {"statement": "public @interface Ann {}"}, [])
        assert d.source == "public @interface Ann {}\n"

    def test_build_class_with_annotations(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "@Deprecated\npublic class Foo {}"}, [])
        assert d.source.startswith("@Deprecated")

    def test_build_class_wrong_keyword_rejected(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {"statement": "public interface Foo {}"}, [])

    def test_build_field(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("field", {"statement": "private int x = 0;"}, [])
        assert d.source == "private int x = 0;\n"

    def test_build_method_minimal(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("method", {"name": "foo"}, [])
        assert d.source == "void foo() {\n}\n"

    def test_build_method_with_return_and_params(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {
                "name": "add",
                "parameters": "int a, int b",
                "return_type": "int",
                "body": "return a + b;",
            },
            [],
        )
        assert "int add(int a, int b)" in d.source
        assert "return a + b;" in d.source

    def test_build_method_with_modifiers(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {"name": "foo", "modifiers": "public static", "return_type": "void"},
            [],
        )
        assert d.source.startswith("public static ")

    def test_build_method_with_annotations(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {
                "name": "toString",
                "annotations": "@Override",
                "modifiers": "public",
                "return_type": "String",
                "body": 'return "";',
            },
            [],
        )
        assert "@Override" in d.source
        assert "public" in d.source

    def test_build_method_with_throws(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {
                "name": "foo",
                "return_type": "void",
                "throws": "java.io.IOException",
            },
            [],
        )
        assert "throws java.io.IOException" in d.source

    def test_build_method_with_generics(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {
                "name": "identity",
                "generics": "T",
                "return_type": "T",
                "parameters": "T x",
                "body": "return x;",
            },
            [],
        )
        assert "<T>" in d.source
        assert "T identity(T x)" in d.source

    def test_build_constructor_minimal(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("constructor", {"name": "Foo", "modifiers": "public"}, [])
        assert "public Foo() {" in d.source

    def test_build_constructor_with_params(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration(
            "constructor",
            {
                "name": "Foo",
                "modifiers": "public",
                "parameters": "int x, String s",
                "body": "this.x = x;",
            },
            [],
        )
        assert "Foo(int x, String s)" in d.source

    def test_build_method_missing_name_raises(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {}, [])

    def test_build_method_wrong_type_raises(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {"name": 42}, [])

    def test_build_source_file_rejected(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("source_file", {}, [])

    def test_build_unknown_kind_raises(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(KeyError):
            backend.build_declaration("not-a-kind", {}, [])

    def test_build_rejects_foreign_child(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {"statement": "public class A {}"}, ["not a declaration"])


# -----------------------------------------------------------------------------
# insert_child
# -----------------------------------------------------------------------------


class TestInsertChild:
    def test_insert_end_into_empty(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        cls = backend.build_declaration("class", {"statement": "public class Foo {}"}, [])
        new = backend.insert_child(tree, cls, position="end")
        assert "public class Foo" in new.source

    def test_insert_end_into_file(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        new = backend.insert_child(tree, cls, position="end")
        assert "public class A" in new.source
        assert "public class B" in new.source

    def test_insert_before_anchor(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\npublic class B {}\n")
        anchor = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "B")
        cls = backend.build_declaration("class", {"statement": "public class Mid {}"}, [])
        new = backend.insert_child(tree, cls, anchor=anchor, position="before")
        assert new.source.index("Mid") < new.source.index("class B")

    def test_insert_after_anchor(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\npublic class B {}\n")
        anchor = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "A")
        cls = backend.build_declaration("class", {"statement": "public class Mid {}"}, [])
        new = backend.insert_child(tree, cls, anchor=anchor, position="after")
        assert new.source.index("class A") < new.source.index("Mid") < new.source.index("class B")

    def test_insert_start_prefixes_source(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        imp = backend.build_declaration("import", {"statement": "import java.util.List;"}, [])
        new = backend.insert_child(tree, imp, position="start")
        assert new.source.index("import") < new.source.index("class A")

    def test_insert_invalid_position_raises(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, cls, position="nowhere")

    def test_insert_before_without_anchor_raises(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, cls, position="before")

    def test_insert_rejects_non_declaration_child(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        with pytest.raises(TypeError):
            backend.insert_child(tree, "not a decl", position="end")

    def test_insert_rejects_non_tree_parent(self, backend: JavaStructuralLanguage) -> None:
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(TypeError):
            backend.insert_child("not a tree", cls, position="end")

    def test_insert_with_symbol_ref_parent_raises(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        ref = next(iter(backend.walk_symbols(tree)))[2]
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(TypeError):
            backend.insert_child(ref, cls, position="end")


# -----------------------------------------------------------------------------
# remove_child
# -----------------------------------------------------------------------------


class TestRemoveChild:
    def test_remove_middle_decl(self, backend: JavaStructuralLanguage) -> None:
        src = "public class A {}\npublic class B {}\npublic class C {}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "B")
        new = backend.remove_child(tree, ref)
        assert "class B" not in new.source
        assert "class A" in new.source
        assert "class C" in new.source

    def test_remove_import(self, backend: JavaStructuralLanguage) -> None:
        src = "import java.util.List;\nimport java.util.Map;\npublic class C {}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "java.util.Map")
        new = backend.remove_child(tree, ref)
        assert "java.util.Map" not in new.source
        assert "java.util.List" in new.source

    def test_remove_method(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n    public void bar() {}\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/foo()")
        new = backend.remove_child(tree, ref)
        assert "foo" not in new.source
        assert "bar" in new.source

    def test_remove_field(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public int x;\n    public int y;\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/x")
        new = backend.remove_child(tree, ref)
        assert "int x" not in new.source
        assert "int y" in new.source

    def test_remove_constructor(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {\n    public C() {}\n    public C(int x) {}\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/C()")
        new = backend.remove_child(tree, ref)
        # One constructor should remain, the no-arg one gone.
        assert "C()" not in new.source.replace("public C(int x)", "")

    def test_remove_missing_raises(self, backend: JavaStructuralLanguage) -> None:
        src = "public class A {}\n"
        tree = backend.parse(src)
        fake = _JavaSymbolRef(
            kind="class",
            name_path="Nope",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_remove_rejects_non_symbol(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        with pytest.raises(TypeError):
            backend.remove_child(tree, "not a ref")


# -----------------------------------------------------------------------------
# empty_source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        assert backend.serialize(tree) == ""

    def test_empty_source_rejects_unknown_kind(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("not-a-source-kind")


# -----------------------------------------------------------------------------
# serialize
# -----------------------------------------------------------------------------


class TestSerialize:
    def test_serialize_tree(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C {}\n"
        assert backend.serialize(backend.parse(src)) == src

    def test_serialize_declaration(self, backend: JavaStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "public class F {}"}, [])
        assert backend.serialize(d) == "public class F {}\n"

    def test_serialize_rejects_bad_type(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.serialize("raw string")


# -----------------------------------------------------------------------------
# Patterns
# -----------------------------------------------------------------------------


class TestPatterns:
    def test_compile_rejects_empty(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_unparseable_pattern_surfaces_on_find(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class C { public void f() { int x = 1; } }\n")
        pat = backend.compile_pattern("@@@@")
        with pytest.raises(PatternError):
            list(backend.find_matches(tree, pat))

    def test_find_exact_call_match(self, backend: JavaStructuralLanguage) -> None:
        src = 'public class C { public void f() { System.out.println("hi"); } }\n'
        tree = backend.parse(src)
        pat = backend.compile_pattern("System.out.println($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert any("hi" in m.bindings.get("msg", "") for m in matches)

    def test_find_wildcard(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C { public void f() { doStuff(1, 2); } }\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("doStuff($_, $_)")
        matches = list(backend.find_matches(tree, pat))
        assert len(matches) >= 1

    def test_find_no_match(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C { public void f() { a.b.c(); } }\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("System.out.println($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert matches == []

    def test_find_respects_repeated_capture(self, backend: JavaStructuralLanguage) -> None:
        src = "public class C { public void f() { eq(a, a); eq(a, b); } }\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("eq($x, $x)")
        matches = list(backend.find_matches(tree, pat))
        # Only the first call has x==x.
        assert len(matches) == 1

    def test_render_replacement(self, backend: JavaStructuralLanguage) -> None:
        repl = backend.render_replacement("log($msg)", {"msg": '"hi"'})
        assert repl.source == 'log("hi")'

    def test_render_replacement_missing_binding(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("log($msg)", {})

    def test_render_replacement_empty_rejected(self, backend: JavaStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("", {})

    def test_apply_replacement_round_trip(self, backend: JavaStructuralLanguage) -> None:
        src = 'public class C { public void f() { log("hi"); } }\n'
        tree = backend.parse(src)
        pat = backend.compile_pattern("log($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert matches
        repl = backend.render_replacement("log2($msg)", {"msg": matches[0].bindings["msg"]})
        new = backend.apply_replacement(tree, matches[0], repl)
        assert "log2" in new.source

    def test_find_matches_rejects_non_tree(self, backend: JavaStructuralLanguage) -> None:
        pat = backend.compile_pattern("foo")
        with pytest.raises(TypeError):
            list(backend.find_matches("not a tree", pat))

    def test_find_matches_rejects_non_pattern(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class C {}\n")
        with pytest.raises(TypeError):
            list(backend.find_matches(tree, "not a pattern"))


# -----------------------------------------------------------------------------
# Registry exposure
# -----------------------------------------------------------------------------


class TestRegistryExposure:
    def test_java_registered(self) -> None:
        registry = default_structural_backend_registry()
        assert "java" in registry.registered_languages()

    def test_java_extension_routed(self) -> None:
        registry = default_structural_backend_registry()
        be = registry.for_relative_path("src/Foo.java")
        assert be is not None
        assert be.language_key == "java"

    def test_java_extension_case_insensitive(self) -> None:
        registry = default_structural_backend_registry()
        be = registry.for_relative_path("Src/Foo.JAVA")
        assert be is not None
        assert be.language_key == "java"


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


class TestErrorMapping:
    def test_remove_missing_symbol_raises(self, backend: JavaStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        fake = _JavaSymbolRef(
            kind="class",
            name_path="DoesNotExist",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_apply_replacement_rejects_non_tree(self, backend: JavaStructuralLanguage) -> None:
        from solidlsp.structural.patterns import PatternMatch as _PM

        repl = backend.render_replacement("x", {})
        ref = _JavaSymbolRef(kind="match", name_path="", extent_offset=0, extent_length=0, body_range=None)
        pm = _PM(node=ref, bindings={}, symbol_path=None)
        with pytest.raises(TypeError):
            backend.apply_replacement("not a tree", pm, repl)

    def test_apply_replacement_rejects_non_declaration(self, backend: JavaStructuralLanguage) -> None:
        from solidlsp.structural.patterns import PatternMatch as _PM

        tree = backend.parse("public class A {}\n")
        ref = _JavaSymbolRef(kind="match", name_path="", extent_offset=0, extent_length=0, body_range=None)
        pm = _PM(node=ref, bindings={}, symbol_path=None)
        with pytest.raises(TypeError):
            backend.apply_replacement(tree, pm, "not a declaration")
