"""Tests for the C# structural backend.

Mirrors the Ruby / Java backends' test shape: curated edge-case fixtures
for round-trip, plus focused coverage of walk_symbols, build_declaration,
insert_child, remove_child, empty_source, and pattern matching over the
Roslyn C# parser via the subprocess bridge.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path as _PathForTests

import pytest

from solidlsp.structural.backends.csharp import (
    CSharpLogicalNameResolver,
    CSharpStructuralLanguage,
    _CSharpSymbolRef,
    csharp_kind_schema,
)
from solidlsp.structural.errors import DeclarationError, NameResolutionError, PatternError
from solidlsp.structural.registry import default_structural_backend_registry
from test.solidlsp.structural.harness import assert_round_trip

# -----------------------------------------------------------------------------
# Shared fixture
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Iterator[CSharpStructuralLanguage]:
    # module-scoped so the subprocess is spun up once per test module
    be = CSharpStructuralLanguage()
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
    ("xml-doc-on-class", "/// <summary>Does something.</summary>\npublic class Foo {}\n"),
    ("attribute-on-class", "[Serializable]\npublic class Foo {}\n"),
    ("using-and-namespace", "using System;\nnamespace A.B {\n    public class C {}\n}\n"),
    ("using-static", "using static System.Math;\npublic class C {}\n"),
    ("using-alias", "using Lst = System.Collections.Generic.List<int>;\npublic class C {}\n"),
    ("global-using", "global using System;\npublic class C {}\n"),
    ("file-scoped-namespace", "namespace A.B;\npublic class C {}\n"),
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
        "interface-with-method",
        "public interface I {\n    int Foo();\n}\n",
    ),
    (
        "generic-class",
        "public class Pair<A, B> {\n    public A first;\n    public B second;\n}\n",
    ),
    (
        "enum-with-values",
        "public enum Color { RED, GREEN, BLUE }\n",
    ),
    (
        "record-simple",
        "public record Point(int X, int Y);\n",
    ),
    (
        "record-struct",
        "public record struct Coord(int X, int Y);\n",
    ),
    (
        "delegate-declaration",
        "public delegate void Handler(int x, string s);\n",
    ),
    (
        "struct-simple",
        "public struct P { public int X; public int Y; }\n",
    ),
    (
        "nested-class",
        "public class Outer {\n    public class Inner {\n        public void f() {}\n    }\n}\n",
    ),
    (
        "generic-method",
        "public class C {\n    public T Identity<T>(T x) { return x; }\n}\n",
    ),
    (
        "method-with-attribute",
        'public class C {\n    [Obsolete]\n    public string ToStr() { return "C"; }\n}\n',
    ),
    (
        "multiple-top-level-declared",
        "public class A {}\npublic class B {}\n",
    ),
    (
        "params-method",
        "public class C {\n    public void foo(params int[] xs) {}\n}\n",
    ),
    (
        "readonly-field",
        "public class C {\n    public readonly int x = 0;\n}\n",
    ),
    (
        "static-field",
        "public class C {\n    public static int count = 0;\n}\n",
    ),
    (
        "const-field",
        "public class C {\n    public const int MAX = 10;\n}\n",
    ),
    (
        "auto-property",
        "public class C {\n    public int Count { get; set; }\n}\n",
    ),
    (
        "expression-bodied-method",
        "public class C {\n    public int Add(int a, int b) => a + b;\n}\n",
    ),
    (
        "event-field",
        "public class C {\n    public event System.EventHandler E;\n}\n",
    ),
    (
        "unicode-identifier",
        'public class C {\n    public string \u00e9t\u00e9 = "summer";\n}\n',
    ),
    (
        "unicode-in-string",
        'public class C {\n    public string s = "caf\u00e9";\n}\n',
    ),
    (
        "multiple-usings",
        "using System;\nusing System.IO;\nusing System.Text;\npublic class C {}\n",
    ),
    (
        "ws-leading",
        "    \npublic class C {}\n",
    ),
    (
        "trailing-whitespace-lines",
        "public class C {}\n   \n  \n",
    ),
    (
        "nested-namespace",
        "namespace A {\n    namespace B {\n        public class C {}\n    }\n}\n",
    ),
    (
        "abstract-class",
        "public abstract class C {\n    public abstract void F();\n}\n",
    ),
    (
        "sealed-class",
        "public sealed class C {}\n",
    ),
    (
        "partial-class",
        "public partial class C {\n    public int X;\n}\n",
    ),
    (
        "async-method",
        "public class C {\n    public async System.Threading.Tasks.Task F() { await System.Threading.Tasks.Task.Delay(1); }\n}\n",
    ),
    (
        "using-block",
        "public class C {\n    public void F() {\n        using (var x = new System.IO.MemoryStream()) { }\n    }\n}\n",
    ),
    (
        "interpolated-string",
        'public class C {\n    public string S(int x) => $"val={x}";\n}\n',
    ),
    (
        "verbatim-string",
        'public class C {\n    public string P = @"C:\\path\\to\\file";\n}\n',
    ),
)


class TestRoundTripFixtures:
    @pytest.mark.parametrize("label,source", _EDGE_CASES, ids=[c[0] for c in _EDGE_CASES])
    def test_edge_case_round_trips(self, backend: CSharpStructuralLanguage, label: str, source: str) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Identity
# -----------------------------------------------------------------------------


class TestIdentity:
    def test_language_key(self, backend: CSharpStructuralLanguage) -> None:
        assert backend.language_key == "csharp"

    def test_kind_schema_identity(self, backend: CSharpStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "csharp"
        assert "source_file" in schema.source_kinds

    def test_name_resolver_is_csharp_resolver(self, backend: CSharpStructuralLanguage, tmp_path: _PathForTests) -> None:
        be = CSharpStructuralLanguage(name_resolver=CSharpLogicalNameResolver(tmp_path))
        assert isinstance(be.name_resolver, CSharpLogicalNameResolver)
        be.close()


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_expected_kinds_present(self) -> None:
        schema = csharp_kind_schema()
        expected = {
            "source_file",
            "using",
            "namespace",
            "class",
            "struct",
            "interface",
            "enum",
            "record",
            "delegate",
            "method",
            "property",
            "field",
            "event",
            "constructor",
        }
        assert set(schema.kinds) == expected

    def test_source_file_children(self) -> None:
        schema = csharp_kind_schema()
        sf = schema.get("source_file")
        assert sf.allowed_child_kinds == frozenset(
            {
                "using",
                "namespace",
                "class",
                "struct",
                "interface",
                "enum",
                "record",
                "delegate",
            }
        )

    def test_using_only_in_source_file(self) -> None:
        schema = csharp_kind_schema()
        u = schema.get("using")
        assert u.allowed_parent_kinds == frozenset({"source_file"})

    def test_namespace_only_in_source_file(self) -> None:
        schema = csharp_kind_schema()
        n = schema.get("namespace")
        assert n.allowed_parent_kinds == frozenset({"source_file"})

    def test_class_only_in_source_file(self) -> None:
        schema = csharp_kind_schema()
        c = schema.get("class")
        assert c.allowed_parent_kinds == frozenset({"source_file"})

    def test_method_not_insertable_in_v1(self) -> None:
        schema = csharp_kind_schema()
        assert schema.get("method").allowed_parent_kinds == frozenset()

    def test_field_not_insertable_in_v1(self) -> None:
        schema = csharp_kind_schema()
        assert schema.get("field").allowed_parent_kinds == frozenset()

    def test_property_not_insertable_in_v1(self) -> None:
        schema = csharp_kind_schema()
        assert schema.get("property").allowed_parent_kinds == frozenset()

    def test_event_not_insertable_in_v1(self) -> None:
        schema = csharp_kind_schema()
        assert schema.get("event").allowed_parent_kinds == frozenset()

    def test_constructor_not_insertable_in_v1(self) -> None:
        schema = csharp_kind_schema()
        assert schema.get("constructor").allowed_parent_kinds == frozenset()

    def test_all_top_level_kinds_are_leaves(self) -> None:
        schema = csharp_kind_schema()
        for kind_name in ("using", "namespace", "class", "struct", "interface", "enum", "record", "delegate"):
            k = schema.get(kind_name)
            assert k.allowed_child_kinds == frozenset(), kind_name

    def test_unknown_kind_raises(self) -> None:
        schema = csharp_kind_schema()
        with pytest.raises(KeyError):
            schema.get("does-not-exist")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_and_resolve_single(self, tmp_path: _PathForTests) -> None:
        resolver = CSharpLogicalNameResolver(tmp_path)
        name = resolver.parse("Foo")
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "Foo.cs"
        assert resolution.source_kind == "source_file"
        assert resolution.exists is False

    def test_parse_and_resolve_dotted(self, tmp_path: _PathForTests) -> None:
        resolver = CSharpLogicalNameResolver(tmp_path)
        (tmp_path / "Foo" / "Bar").mkdir(parents=True)
        (tmp_path / "Foo" / "Bar" / "Baz.cs").write_text("public class Baz {}\n")
        name = resolver.parse("Foo.Bar.Baz")
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "Foo/Bar/Baz.cs"
        assert resolution.exists is True

    def test_empty_name_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = CSharpLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("")

    def test_invalid_segment_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = CSharpLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("foo..bar")

    def test_invalid_character_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = CSharpLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("foo-bar")

    def test_dollar_identifier_rejected(self, tmp_path: _PathForTests) -> None:
        # C# identifiers disallow $, unlike Java.
        resolver = CSharpLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("Foo$Inner")


# -----------------------------------------------------------------------------
# Root kind
# -----------------------------------------------------------------------------


class TestRootKind:
    def test_root_kind_is_source_file(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class C {}\n")
        assert backend.root_kind(tree) == "source_file"

    def test_root_kind_rejects_non_tree(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.root_kind("not a tree")


# -----------------------------------------------------------------------------
# walk_symbols
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_class(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class Foo {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Foo", "class") in names

    def test_walks_using(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("using System;\npublic class C {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("System", "using") in names

    def test_walks_using_static(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("using static System.Math;\npublic class C {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("static System.Math", "using") in names

    def test_walks_using_alias(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("using Lst = System.Collections.Generic.List<int>;\npublic class C {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Lst", "using") in names

    def test_walks_global_using(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("global using System;\npublic class C {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("global System", "using") in names

    def test_walks_block_namespace(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("namespace A.B {\n    public class C {}\n}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("A.B", "namespace") in names
        assert ("A.B/C", "class") in names

    def test_walks_file_scoped_namespace(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("namespace A.B;\npublic class C {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("A.B", "namespace") in names
        assert ("A.B/C", "class") in names

    def test_walks_nested_namespaces(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("namespace A {\n    namespace B {\n        public class C {}\n    }\n}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("A", "namespace") in names
        assert ("A/B", "namespace") in names
        assert ("A/B/C", "class") in names

    def test_walks_interface(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public interface I {}\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("I", "interface") in names

    def test_walks_struct(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public struct P { public int X; }\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("P", "struct") in names

    def test_walks_enum(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public enum E { A, B, C }\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("E", "enum") in names

    def test_walks_record(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public record P(int X, int Y);\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("P", "record") in names

    def test_walks_record_struct_as_record(self, backend: CSharpStructuralLanguage) -> None:
        # We collapse record struct into the single "record" kind.
        tree = backend.parse("public record struct C(int X);\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C", "record") in names

    def test_walks_delegate(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public delegate void Handler(int x, string s);\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Handler(int,string)", "delegate") in names

    def test_walks_method(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/foo()", "method") in names

    def test_walks_method_with_params(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public int add(int a, int b) { return 0; }\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/add(int,int)", "method") in names

    def test_walks_field(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public int x;\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/x", "field") in names

    def test_walks_multi_var_field(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public int a, b, c;\n}\n"
        tree = backend.parse(src)
        fields = [(n, k) for (n, k, _) in backend.walk_symbols(tree) if k == "field"]
        assert {("C/a", "field"), ("C/b", "field"), ("C/c", "field")} == set(fields)

    def test_walks_property(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public int X { get; set; }\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/X", "property") in names

    def test_walks_event_field(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public event System.EventHandler E;\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/E", "event") in names

    def test_walks_event_accessor(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public event System.EventHandler E { add { } remove { } }\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/E", "event") in names

    def test_walks_constructor(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public C(int x) {}\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/C(int)", "constructor") in names

    def test_walks_nested_class(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class Outer {\n    public class Inner {\n        public void f() {}\n    }\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Outer", "class") in names
        assert ("Outer/Inner", "class") in names
        assert ("Outer/Inner/f()", "method") in names

    def test_walks_overloaded_methods_disambiguated(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n    public void foo(int x) {}\n    public void foo(int x, string s) {}\n}\n"
        tree = backend.parse(src)
        names = [n for (n, k, _) in backend.walk_symbols(tree) if k == "method"]
        assert "C/foo()" in names
        assert "C/foo(int)" in names
        assert "C/foo(int,string)" in names

    def test_walks_params_method(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public void foo(params int[] xs) {}\n}\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/foo(int[]...)", "method") in names

    def test_walk_extents_point_into_source(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n}\n"
        tree = backend.parse(src)
        for name_path, _kind, ref in backend.walk_symbols(tree):
            assert isinstance(ref, _CSharpSymbolRef)
            sliced = src.encode("utf-8")[ref.extent_offset : ref.extent_offset + ref.extent_length].decode("utf-8")
            if name_path == "C":
                assert "class C" in sliced
            if name_path == "C/foo()":
                assert "foo" in sliced

    def test_walk_body_range_is_none_v1(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n}\n"
        tree = backend.parse(src)
        for _n, _k, ref in backend.walk_symbols(tree):
            assert ref.body_range is None

    def test_walk_symbols_empty_source(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("")
        assert list(backend.walk_symbols(tree)) == []

    def test_walk_symbols_rejects_non_tree(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            list(backend.walk_symbols("not a tree"))

    def test_walk_unicode_identifier(self, backend: CSharpStructuralLanguage) -> None:
        src = 'public class C {\n    public string \u00e9t\u00e9 = "s";\n}\n'
        tree = backend.parse(src)
        names = [n for (n, _k, _r) in backend.walk_symbols(tree)]
        assert "C/\u00e9t\u00e9" in names

    def test_walk_top_level_statements_ignored(self, backend: CSharpStructuralLanguage) -> None:
        # Top-level statements are not modeled as symbols in v1.
        tree = backend.parse("var x = 1;\n")
        entries = list(backend.walk_symbols(tree))
        # May be empty or may emit garbage depending on Roslyn's reading;
        # the requirement is only that no spurious class/method appears.
        assert all(k not in ("class", "method") for (_n, k, _r) in entries)


# -----------------------------------------------------------------------------
# build_declaration
# -----------------------------------------------------------------------------


class TestBuildDeclaration:
    def test_build_using(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("using", {"statement": "using System;"}, [])
        assert d.source == "using System;\n"

    def test_build_using_requires_keyword(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("using", {"statement": "System;"}, [])

    def test_build_using_static(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("using", {"statement": "using static System.Math;"}, [])
        assert "static" in d.source

    def test_build_global_using(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("using", {"statement": "global using System;"}, [])
        assert d.source == "global using System;\n"

    def test_build_namespace(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("namespace", {"statement": "namespace A.B { }"}, [])
        assert d.source == "namespace A.B { }\n"

    def test_build_namespace_requires_keyword(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("namespace", {"statement": "A.B { }"}, [])

    def test_build_class(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "public class Foo {}"}, [])
        assert d.source == "public class Foo {}\n"

    def test_build_struct(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("struct", {"statement": "public struct P {}"}, [])
        assert d.source == "public struct P {}\n"

    def test_build_interface(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("interface", {"statement": "public interface I {}"}, [])
        assert d.source == "public interface I {}\n"

    def test_build_enum(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("enum", {"statement": "public enum E { A, B }"}, [])
        assert d.source == "public enum E { A, B }\n"

    def test_build_record(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("record", {"statement": "public record P(int x);"}, [])
        assert d.source == "public record P(int x);\n"

    def test_build_record_struct(self, backend: CSharpStructuralLanguage) -> None:
        # record struct still starts with 'record' keyword; the require_starts_with
        # logic accepts it.
        d = backend.build_declaration("record", {"statement": "public record struct P(int x);"}, [])
        assert "record struct" in d.source

    def test_build_delegate(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("delegate", {"statement": "public delegate void Handler();"}, [])
        assert d.source == "public delegate void Handler();\n"

    def test_build_class_with_attributes(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "[Serializable]\npublic class Foo {}"}, [])
        assert d.source.startswith("[Serializable]")

    def test_build_class_wrong_keyword_rejected(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {"statement": "public interface Foo {}"}, [])

    def test_build_field(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("field", {"statement": "private int x = 0;"}, [])
        assert d.source == "private int x = 0;\n"

    def test_build_property(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("property", {"statement": "public int X { get; set; }"}, [])
        assert d.source == "public int X { get; set; }\n"

    def test_build_event(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("event", {"statement": "public event System.EventHandler E;"}, [])
        assert d.source == "public event System.EventHandler E;\n"

    def test_build_method_minimal(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("method", {"name": "foo"}, [])
        assert d.source == "void foo() {\n}\n"

    def test_build_method_with_return_and_params(self, backend: CSharpStructuralLanguage) -> None:
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

    def test_build_method_with_modifiers(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {"name": "foo", "modifiers": "public static", "return_type": "void"},
            [],
        )
        assert d.source.startswith("public static ")

    def test_build_method_with_attributes(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {
                "name": "ToStr",
                "attributes": "[Obsolete]",
                "modifiers": "public",
                "return_type": "string",
                "body": 'return "C";',
            },
            [],
        )
        assert "[Obsolete]" in d.source
        assert "public" in d.source

    def test_build_method_with_generics(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {
                "name": "Identity",
                "generics": "T",
                "return_type": "T",
                "parameters": "T x",
                "body": "return x;",
            },
            [],
        )
        # In C#, generics attach to the method name: `T Identity<T>(T x)`.
        assert "<T>" in d.source
        assert "T Identity<T>(T x)" in d.source

    def test_build_constructor_minimal(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("constructor", {"name": "Foo", "modifiers": "public"}, [])
        assert "public Foo() {" in d.source

    def test_build_constructor_with_params(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration(
            "constructor",
            {
                "name": "Foo",
                "modifiers": "public",
                "parameters": "int x, string s",
                "body": "this.x = x;",
            },
            [],
        )
        assert "Foo(int x, string s)" in d.source

    def test_build_method_missing_name_raises(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {}, [])

    def test_build_method_wrong_type_raises(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {"name": 42}, [])

    def test_build_source_file_rejected(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("source_file", {}, [])

    def test_build_unknown_kind_raises(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(KeyError):
            backend.build_declaration("not-a-kind", {}, [])

    def test_build_rejects_foreign_child(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {"statement": "public class A {}"}, ["not a declaration"])


# -----------------------------------------------------------------------------
# insert_child
# -----------------------------------------------------------------------------


class TestInsertChild:
    def test_insert_end_into_empty(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        cls = backend.build_declaration("class", {"statement": "public class Foo {}"}, [])
        new = backend.insert_child(tree, cls, position="end")
        assert "public class Foo" in new.source

    def test_insert_end_into_file(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        new = backend.insert_child(tree, cls, position="end")
        assert "public class A" in new.source
        assert "public class B" in new.source

    def test_insert_before_anchor(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\npublic class B {}\n")
        anchor = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "B")
        cls = backend.build_declaration("class", {"statement": "public class Mid {}"}, [])
        new = backend.insert_child(tree, cls, anchor=anchor, position="before")
        assert new.source.index("Mid") < new.source.index("class B")

    def test_insert_after_anchor(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\npublic class B {}\n")
        anchor = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "A")
        cls = backend.build_declaration("class", {"statement": "public class Mid {}"}, [])
        new = backend.insert_child(tree, cls, anchor=anchor, position="after")
        assert new.source.index("class A") < new.source.index("Mid") < new.source.index("class B")

    def test_insert_start_prefixes_source(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        u = backend.build_declaration("using", {"statement": "using System;"}, [])
        new = backend.insert_child(tree, u, position="start")
        assert new.source.index("using") < new.source.index("class A")

    def test_insert_invalid_position_raises(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, cls, position="nowhere")

    def test_insert_before_without_anchor_raises(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, cls, position="before")

    def test_insert_rejects_non_declaration_child(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        with pytest.raises(TypeError):
            backend.insert_child(tree, "not a decl", position="end")

    def test_insert_rejects_non_tree_parent(self, backend: CSharpStructuralLanguage) -> None:
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(TypeError):
            backend.insert_child("not a tree", cls, position="end")

    def test_insert_with_symbol_ref_parent_raises(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        ref = next(iter(backend.walk_symbols(tree)))[2]
        cls = backend.build_declaration("class", {"statement": "public class B {}"}, [])
        with pytest.raises(TypeError):
            backend.insert_child(ref, cls, position="end")


# -----------------------------------------------------------------------------
# remove_child
# -----------------------------------------------------------------------------


class TestRemoveChild:
    def test_remove_middle_decl(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class A {}\npublic class B {}\npublic class C {}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "B")
        new = backend.remove_child(tree, ref)
        assert "class B" not in new.source
        assert "class A" in new.source
        assert "class C" in new.source

    def test_remove_using(self, backend: CSharpStructuralLanguage) -> None:
        src = "using System;\nusing System.IO;\npublic class C {}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "System.IO")
        new = backend.remove_child(tree, ref)
        assert "System.IO" not in new.source
        assert "using System;\n" in new.source

    def test_remove_method(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public void foo() {}\n    public void bar() {}\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/foo()")
        new = backend.remove_child(tree, ref)
        assert "foo" not in new.source
        assert "bar" in new.source

    def test_remove_field(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public int x;\n    public int y;\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/x")
        new = backend.remove_child(tree, ref)
        assert "int x" not in new.source
        assert "int y" in new.source

    def test_remove_constructor(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public C() {}\n    public C(int x) {}\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/C()")
        new = backend.remove_child(tree, ref)
        assert "C()" not in new.source.replace("public C(int x)", "")

    def test_remove_property(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {\n    public int X { get; set; }\n    public int Y { get; set; }\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/X")
        new = backend.remove_child(tree, ref)
        assert "int X" not in new.source
        assert "int Y" in new.source

    def test_remove_namespace(self, backend: CSharpStructuralLanguage) -> None:
        src = "namespace A {\n    public class C {}\n}\nnamespace B {\n    public class D {}\n}\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "A" and _k == "namespace")
        new = backend.remove_child(tree, ref)
        assert "namespace A" not in new.source
        assert "namespace B" in new.source

    def test_remove_missing_raises(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class A {}\n"
        tree = backend.parse(src)
        fake = _CSharpSymbolRef(
            kind="class",
            name_path="Nope",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_remove_rejects_non_symbol(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        with pytest.raises(TypeError):
            backend.remove_child(tree, "not a ref")


# -----------------------------------------------------------------------------
# empty_source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        assert backend.serialize(tree) == ""

    def test_empty_source_rejects_unknown_kind(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("not-a-source-kind")


# -----------------------------------------------------------------------------
# serialize
# -----------------------------------------------------------------------------


class TestSerialize:
    def test_serialize_tree(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C {}\n"
        assert backend.serialize(backend.parse(src)) == src

    def test_serialize_declaration(self, backend: CSharpStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "public class F {}"}, [])
        assert backend.serialize(d) == "public class F {}\n"

    def test_serialize_rejects_bad_type(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.serialize("raw string")


# -----------------------------------------------------------------------------
# Patterns
# -----------------------------------------------------------------------------


class TestPatterns:
    def test_compile_rejects_empty(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_unparseable_pattern_surfaces_on_find(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class C { public void f() { int x = 1; } }\n")
        pat = backend.compile_pattern("@@@@")
        with pytest.raises(PatternError):
            list(backend.find_matches(tree, pat))

    def test_find_exact_call_match(self, backend: CSharpStructuralLanguage) -> None:
        src = 'public class C { public void f() { Log.Info("hi"); } }\n'
        tree = backend.parse(src)
        pat = backend.compile_pattern("Log.Info($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert any("hi" in m.bindings.get("msg", "") for m in matches)

    def test_find_wildcard(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C { public void f() { DoStuff(1, 2); } }\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("DoStuff($_, $_)")
        matches = list(backend.find_matches(tree, pat))
        assert len(matches) >= 1

    def test_find_no_match(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C { public void f() { a.b.C(); } }\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("Log.Info($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert matches == []

    def test_find_respects_repeated_capture(self, backend: CSharpStructuralLanguage) -> None:
        src = "public class C { public void f() { eq(a, a); eq(a, b); } }\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("eq($x, $x)")
        matches = list(backend.find_matches(tree, pat))
        # Only the first call has x==x.
        assert len(matches) == 1

    def test_find_method_name_discriminates(self, backend: CSharpStructuralLanguage) -> None:
        """Regression: a pattern with a concrete method name must not match
        calls with a different name. This caught a bug in Ruby during the
        smoke-test phase and guards the same class of bug for C#.
        """
        src = "public class C { public void f() { Foo(1); Bar(2); } }\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("Foo($x)")
        matches = list(backend.find_matches(tree, pat))
        # Only Foo(1), not Bar(2).
        assert len(matches) == 1
        assert matches[0].bindings["x"] == "1"

    def test_render_replacement(self, backend: CSharpStructuralLanguage) -> None:
        repl = backend.render_replacement("Log($msg)", {"msg": '"hi"'})
        assert repl.source == 'Log("hi")'

    def test_render_replacement_missing_binding(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("Log($msg)", {})

    def test_render_replacement_empty_rejected(self, backend: CSharpStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("", {})

    def test_apply_replacement_round_trip(self, backend: CSharpStructuralLanguage) -> None:
        src = 'public class C { public void f() { Log("hi"); } }\n'
        tree = backend.parse(src)
        pat = backend.compile_pattern("Log($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert matches
        repl = backend.render_replacement("Log2($msg)", {"msg": matches[0].bindings["msg"]})
        new = backend.apply_replacement(tree, matches[0], repl)
        assert "Log2" in new.source

    def test_find_matches_rejects_non_tree(self, backend: CSharpStructuralLanguage) -> None:
        pat = backend.compile_pattern("foo")
        with pytest.raises(TypeError):
            list(backend.find_matches("not a tree", pat))

    def test_find_matches_rejects_non_pattern(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class C {}\n")
        with pytest.raises(TypeError):
            list(backend.find_matches(tree, "not a pattern"))

    def test_find_matches_statement_pattern_yields_none(self, backend: CSharpStructuralLanguage) -> None:
        # A pattern that's a valid statement (but not an expression) compiles
        # but matches nothing in v1 -- no error.
        tree = backend.parse("public class C { public void f() { Log(1); } }\n")
        pat = backend.compile_pattern("var x = 1;")
        matches = list(backend.find_matches(tree, pat))
        assert matches == []


# -----------------------------------------------------------------------------
# Registry exposure
# -----------------------------------------------------------------------------


class TestRegistryExposure:
    def test_csharp_registered(self) -> None:
        registry = default_structural_backend_registry()
        assert "csharp" in registry.registered_languages()

    def test_csharp_extension_routed(self) -> None:
        registry = default_structural_backend_registry()
        be = registry.for_relative_path("src/Foo.cs")
        assert be is not None
        assert be.language_key == "csharp"

    def test_csharp_extension_case_insensitive(self) -> None:
        registry = default_structural_backend_registry()
        be = registry.for_relative_path("Src/Foo.CS")
        assert be is not None
        assert be.language_key == "csharp"


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


class TestErrorMapping:
    def test_remove_missing_symbol_raises(self, backend: CSharpStructuralLanguage) -> None:
        tree = backend.parse("public class A {}\n")
        fake = _CSharpSymbolRef(
            kind="class",
            name_path="DoesNotExist",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_apply_replacement_rejects_non_tree(self, backend: CSharpStructuralLanguage) -> None:
        from solidlsp.structural.patterns import PatternMatch as _PM

        repl = backend.render_replacement("x", {})
        ref = _CSharpSymbolRef(kind="match", name_path="", extent_offset=0, extent_length=0, body_range=None)
        pm = _PM(node=ref, bindings={}, symbol_path=None)
        with pytest.raises(TypeError):
            backend.apply_replacement("not a tree", pm, repl)

    def test_apply_replacement_rejects_non_declaration(self, backend: CSharpStructuralLanguage) -> None:
        from solidlsp.structural.patterns import PatternMatch as _PM

        tree = backend.parse("public class A {}\n")
        ref = _CSharpSymbolRef(kind="match", name_path="", extent_offset=0, extent_length=0, body_range=None)
        pm = _PM(node=ref, bindings={}, symbol_path=None)
        with pytest.raises(TypeError):
            backend.apply_replacement(tree, pm, "not a declaration")
