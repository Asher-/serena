"""Tests for the Go structural backend.

Mirrors the TypeScript backend's test shape: curated edge-case fixtures for
round-trip, plus focused coverage of walk_symbols, build_declaration,
insert_child, remove_child, empty_source, and pattern matching over the
Go ``go/ast`` parser via the subprocess bridge.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path as _PathForTests

import pytest

from solidlsp.structural.backends.go import (
    GoLogicalNameResolver,
    GoStructuralLanguage,
    _GoSymbolRef,
    _GoTree,
    go_kind_schema,
)
from solidlsp.structural.errors import DeclarationError, NameResolutionError, PatternError
from solidlsp.structural.registry import default_structural_backend_registry
from test.solidlsp.structural.harness import assert_round_trip

# -----------------------------------------------------------------------------
# Shared fixture
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Iterator[GoStructuralLanguage]:
    # module-scoped so the subprocess is spun up once per test module
    be = GoStructuralLanguage()
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
    ("package-only", "package main\n"),
    ("no-final-newline", "package main"),
    ("crlf-line-endings", "package p\r\nvar x = 1\r\n"),
    ("mixed-line-endings", "package p\nvar x = 1\r\n"),
    ("line-comments-only", "// a comment\n// another\n"),
    ("block-comment-only", "/* a block\n   comment */\n"),
    ("build-tag-comment", "//go:build darwin\n\npackage p\n"),
    (
        "doc-comment-on-func",
        "package p\n\n// Foo does something.\nfunc Foo() {}\n",
    ),
    (
        "import-and-func",
        'package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("hi")\n}\n',
    ),
    (
        "grouped-imports",
        'package main\n\nimport (\n\t"fmt"\n\t"strings"\n)\n',
    ),
    (
        "struct-with-methods",
        "package p\n\ntype Point struct {\n\tx int\n\ty int\n}\n\nfunc (p Point) Norm() int { return p.x*p.x + p.y*p.y }\n",
    ),
    (
        "generic-function",
        "package p\n\nfunc Id[T any](v T) T { return v }\n",
    ),
    (
        "generic-method",
        "package p\n\ntype Box[T any] struct{ v T }\n\nfunc (b Box[T]) Get() T { return b.v }\n",
    ),
    (
        "iota-enum",
        "package p\n\nconst (\n\tRed   = iota\n\tGreen\n\tBlue\n)\n",
    ),
    (
        "interface-decl",
        "package p\n\ntype Greeter interface {\n\tGreet() string\n}\n",
    ),
    (
        "embedded-interface",
        "package p\n\ntype R interface {\n\tRead(p []byte) (n int, err error)\n}\n\ntype RW interface {\n\tR\n\tWrite(p []byte) (n int, err error)\n}\n",
    ),
    (
        "string-with-escapes",
        'package p\n\nvar s = "hello\\n\\t\\"world\\""\n',
    ),
    (
        "raw-string",
        "package p\n\nvar r = `C:\\path\\to\\file`\n",
    ),
    (
        "rune-literal",
        "package p\n\nvar r = 'A'\nvar r2 = '\\n'\n",
    ),
    (
        "channel-ops",
        "package p\n\nfunc f(ch chan int) {\n\tch <- 1\n\tx := <-ch\n\t_ = x\n}\n",
    ),
    (
        "select-stmt",
        "package p\n\nfunc f(a, b chan int) {\n\tselect {\n\tcase x := <-a:\n\t\t_ = x\n\tcase b <- 1:\n\tdefault:\n\t}\n}\n",
    ),
    (
        "defer-and-go",
        "package p\n\nfunc f() {\n\tdefer g()\n\tgo g()\n}\n\nfunc g() {}\n",
    ),
    (
        "type-assertion",
        "package p\n\nfunc f(x any) int {\n\tif v, ok := x.(int); ok {\n\t\treturn v\n\t}\n\treturn 0\n}\n",
    ),
    (
        "map-and-slice",
        'package p\n\nvar m = map[string]int{"a": 1, "b": 2}\nvar s = []int{1, 2, 3}\n',
    ),
    (
        "nested-func-literal",
        "package p\n\nfunc f() {\n\tg := func(x int) int { return x + 1 }\n\t_ = g(1)\n}\n",
    ),
    (
        "multi-return",
        "package p\n\nfunc divmod(a, b int) (int, int) { return a / b, a % b }\n",
    ),
    (
        "variadic",
        "package p\n\nfunc sum(xs ...int) int {\n\ts := 0\n\tfor _, x := range xs {\n\t\ts += x\n\t}\n\treturn s\n}\n",
    ),
    (
        "type-alias-and-defn",
        "package p\n\ntype ID = int\ntype UserID int\n",
    ),
    (
        "struct-tags",
        'package p\n\ntype User struct {\n\tName string `json:"name"`\n\tAge  int    `json:"age"`\n}\n',
    ),
)


class TestRoundTripFixtures:
    @pytest.mark.parametrize(("label", "source"), _EDGE_CASES, ids=[case[0] for case in _EDGE_CASES])
    def test_edge_case_round_trips(
        self,
        backend: GoStructuralLanguage,
        label: str,
        source: str,
    ) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Identity
# -----------------------------------------------------------------------------


class TestIdentity:
    def test_language_key(self, backend: GoStructuralLanguage) -> None:
        assert backend.language_key == "go"

    def test_kind_schema_identity(self, backend: GoStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "go"
        assert schema.source_kinds == frozenset({"source_file"})

    def test_name_resolver_is_go_resolver(self, backend: GoStructuralLanguage) -> None:
        assert isinstance(backend.name_resolver, GoLogicalNameResolver)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_expected_kinds_present(self) -> None:
        schema = go_kind_schema()
        expected = {"source_file", "package", "import", "const", "variable", "type", "function", "method"}
        assert set(schema.kinds) == expected

    def test_source_file_children(self) -> None:
        schema = go_kind_schema()
        sf = schema.get("source_file")
        assert sf.allowed_parent_kinds == frozenset()
        assert sf.allowed_child_kinds == frozenset({"package", "import", "const", "variable", "type", "function", "method"})

    def test_package_only_in_source_file(self) -> None:
        schema = go_kind_schema()
        schema.validate_composition("source_file", "package")
        with pytest.raises(DeclarationError):
            schema.validate_composition("function", "package")

    def test_import_only_in_source_file(self) -> None:
        schema = go_kind_schema()
        schema.validate_composition("source_file", "import")
        with pytest.raises(DeclarationError):
            schema.validate_composition("function", "import")

    def test_function_only_in_source_file(self) -> None:
        schema = go_kind_schema()
        schema.validate_composition("source_file", "function")
        with pytest.raises(DeclarationError):
            schema.validate_composition("type", "function")

    def test_method_only_in_source_file(self) -> None:
        schema = go_kind_schema()
        schema.validate_composition("source_file", "method")
        with pytest.raises(DeclarationError):
            schema.validate_composition("type", "method")

    def test_const_and_variable_siblings(self) -> None:
        schema = go_kind_schema()
        schema.validate_composition("source_file", "const")
        schema.validate_composition("source_file", "variable")

    def test_all_top_level_kinds_are_leaves(self) -> None:
        schema = go_kind_schema()
        for name in ("package", "import", "const", "variable", "type", "function", "method"):
            assert schema.get(name).allowed_child_kinds == frozenset()

    def test_unknown_kind_raises(self) -> None:
        schema = go_kind_schema()
        with pytest.raises(KeyError):
            schema.get("class")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_and_resolve_single(self, tmp_path: _PathForTests) -> None:
        resolver = GoLogicalNameResolver(tmp_path)
        name = resolver.parse("mod")
        assert name.raw == "mod"
        assert name.parts == ("mod",)
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "mod.go"
        assert resolution.source_kind == "source_file"
        assert resolution.exists is False

    def test_parse_and_resolve_dotted(self, tmp_path: _PathForTests) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "util.go").write_text("package pkg\n")
        resolver = GoLogicalNameResolver(tmp_path)
        resolution = resolver.resolve(resolver.parse("pkg.util"))
        assert resolution.relative_path == "pkg/util.go"
        assert resolution.exists is True

    def test_empty_name_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = GoLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("")

    def test_invalid_segment_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = GoLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("pkg.3util")

    def test_invalid_character_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = GoLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("pkg.util-helper")


# -----------------------------------------------------------------------------
# Root kind
# -----------------------------------------------------------------------------


class TestRootKind:
    def test_root_kind_is_source_file(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("")
        assert backend.root_kind(tree) == "source_file"

    def test_root_kind_rejects_non_tree(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.root_kind("not a tree")


# -----------------------------------------------------------------------------
# Walk symbols
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_package_clause(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package main\n")
        symbols = list(backend.walk_symbols(tree))
        kinds = {np: kind for np, kind, _ in symbols}
        assert kinds == {"main": "package"}

    def test_walks_imports(self, backend: GoStructuralLanguage) -> None:
        src = 'package p\n\nimport "fmt"\nimport "strings"\n'
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["p"] == "package"
        assert kinds["fmt"] == "import"
        assert kinds["strings"] == "import"

    def test_walks_grouped_import(self, backend: GoStructuralLanguage) -> None:
        src = 'package p\n\nimport (\n\t"fmt"\n\t"os"\n)\n'
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["fmt+os"] == "import"

    def test_walks_const_var_type(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nconst A = 1\nvar b = 2\ntype T int\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["A"] == "const"
        assert kinds["b"] == "variable"
        assert kinds["T"] == "type"

    def test_walks_grouped_type_decl(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\ntype (\n\tA int\n\tB string\n)\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["A+B"] == "type"

    def test_walks_multi_binding_var(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nvar a, b = 1, 2\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["a+b"] == "variable"

    def test_walks_top_level_function(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nfunc topLevel() {}\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["topLevel"] == "function"

    def test_walks_methods_with_receiver_path(self, backend: GoStructuralLanguage) -> None:
        src = (
            "package p\n\ntype Point struct{ x, y int }\n\nfunc (p Point) X() int { return p.x }\nfunc (p *Point) SetX(x int) { p.x = x }\n"
        )
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["Point"] == "type"
        assert kinds["Point/X"] == "method"
        assert kinds["Point/SetX"] == "method"

    def test_walks_generic_method_receiver(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\ntype Box[T any] struct{ v T }\n\nfunc (b Box[T]) Get() T { return b.v }\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["Box/Get"] == "method"

    def test_walk_extents_point_into_source(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nfunc foo() {}\n"
        tree = backend.parse(src)
        entries = list(backend.walk_symbols(tree))
        ref = next(r for np, k, r in entries if np == "foo")
        assert src[ref.extent_offset : ref.extent_offset + ref.extent_length].startswith("func foo()")

    def test_walk_body_range_is_none_v1(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\ntype Foo struct{ x int }\n"
        tree = backend.parse(src)
        refs = [r for _, _, r in backend.walk_symbols(tree)]
        for r in refs:
            assert r.body_range is None

    def test_walk_symbols_empty_source(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("")
        assert list(backend.walk_symbols(tree)) == []

    def test_walk_symbols_rejects_non_tree(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            list(backend.walk_symbols("not a tree"))


# -----------------------------------------------------------------------------
# Declaration building
# -----------------------------------------------------------------------------


class TestBuildDeclaration:
    def test_build_package(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration("package", {"name": "main"}, [])
        assert decl.source == "package main\n"
        assert decl.kind == "package"

    def test_build_import(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration("import", {"statement": 'import "fmt"'}, [])
        assert decl.source == 'import "fmt"\n'

    def test_build_import_requires_import_keyword(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("import", {"statement": '"fmt"'}, [])

    def test_build_const(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration("const", {"statement": "const X = 1"}, [])
        assert decl.source == "const X = 1\n"

    def test_build_variable(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration("variable", {"statement": "var x = 42"}, [])
        assert decl.source == "var x = 42\n"

    def test_build_variable_requires_var_keyword(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("variable", {"statement": "x := 1"}, [])

    def test_build_type(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration("type", {"statement": "type T int"}, [])
        assert decl.source == "type T int\n"

    def test_build_function_minimal(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "function",
            {"name": "foo", "body": ""},
            [],
        )
        assert decl.source == "func foo() {\n}\n"

    def test_build_function_with_params_and_result(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "function",
            {
                "name": "add",
                "parameters": "a, b int",
                "result": "int",
                "body": "\treturn a + b\n",
            },
            [],
        )
        assert decl.source == "func add(a, b int) int {\n\treturn a + b\n}\n"

    def test_build_function_with_multi_result(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "function",
            {
                "name": "divmod",
                "parameters": "a, b int",
                "result": "(int, int)",
                "body": "\treturn a / b, a % b\n",
            },
            [],
        )
        assert "func divmod(a, b int) (int, int) {" in decl.source

    def test_build_generic_function(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "function",
            {
                "name": "id",
                "type_parameters": "T any",
                "parameters": "v T",
                "result": "T",
                "body": "\treturn v\n",
            },
            [],
        )
        assert decl.source == "func id[T any](v T) T {\n\treturn v\n}\n"

    def test_build_method(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "method",
            {
                "name": "Area",
                "receiver": "r Rect",
                "result": "int",
                "body": "\treturn r.w * r.h\n",
            },
            [],
        )
        assert decl.source == "func (r Rect) Area() int {\n\treturn r.w * r.h\n}\n"

    def test_build_method_pointer_receiver(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "method",
            {
                "name": "Scale",
                "receiver": "r *Rect",
                "parameters": "f int",
                "body": "\tr.w *= f\n",
            },
            [],
        )
        assert decl.source == "func (r *Rect) Scale(f int) {\n\tr.w *= f\n}\n"

    def test_build_declaration_missing_required_raises(
        self,
        backend: GoStructuralLanguage,
    ) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("function", {}, [])

    def test_build_declaration_wrong_type_raises(
        self,
        backend: GoStructuralLanguage,
    ) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("function", {"name": 42, "body": ""}, [])

    def test_build_source_file_rejected(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("source_file", {}, [])

    def test_build_unknown_kind_raises(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(KeyError):
            backend.build_declaration("interface", {"name": "I"}, [])

    def test_build_rejects_foreign_child(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("function", {"name": "f", "body": ""}, ["not a decl"])


# -----------------------------------------------------------------------------
# Insert child
# -----------------------------------------------------------------------------


class TestInsertChild:
    def test_insert_end_into_empty(self, backend: GoStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        pkg = backend.build_declaration("package", {"name": "main"}, [])
        new = backend.insert_child(tree, pkg, anchor=None, position="end")
        assert backend.serialize(new) == "package main\n"

    def test_insert_end_into_file(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        decl = backend.build_declaration("function", {"name": "foo", "body": ""}, [])
        new = backend.insert_child(tree, decl, anchor=None, position="end")
        assert backend.serialize(new) == "package p\nfunc foo() {\n}\n"

    def test_insert_before_anchor(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n\nconst A = 1\nconst C = 3\n")
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        b_decl = backend.build_declaration("const", {"statement": "const B = 2"}, [])
        new = backend.insert_child(tree, b_decl, anchor=symbols["C"], position="before")
        out = backend.serialize(new)
        assert "const A = 1\nconst B = 2\nconst C = 3\n" in out

    def test_insert_after_anchor(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n\nconst A = 1\nconst C = 3\n")
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        b_decl = backend.build_declaration("const", {"statement": "const B = 2"}, [])
        new = backend.insert_child(tree, b_decl, anchor=symbols["A"], position="after")
        out = backend.serialize(new)
        assert "const A = 1\nconst B = 2\nconst C = 3\n" in out

    def test_insert_start_prefixes_source(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        decl = backend.build_declaration("const", {"statement": "const X = 1"}, [])
        new = backend.insert_child(tree, decl, anchor=None, position="start")
        assert backend.serialize(new).startswith("const X = 1\n")

    def test_insert_invalid_position_raises(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        decl = backend.build_declaration("const", {"statement": "const X = 1"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, decl, position="between")

    def test_insert_before_without_anchor_raises(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        decl = backend.build_declaration("const", {"statement": "const X = 1"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, decl, anchor=None, position="before")

    def test_insert_rejects_non_declaration_child(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        with pytest.raises(TypeError):
            backend.insert_child(tree, "raw string", anchor=None, position="end")

    def test_insert_rejects_non_tree_parent(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration("const", {"statement": "const X = 1"}, [])
        with pytest.raises(TypeError):
            backend.insert_child("not a tree", decl, anchor=None, position="end")

    def test_insert_with_symbol_ref_parent_raises(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n\nfunc foo() {}\n")
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        decl = backend.build_declaration("const", {"statement": "const X = 1"}, [])
        with pytest.raises(TypeError):
            backend.insert_child(symbols["foo"], decl, anchor=None, position="end")


# -----------------------------------------------------------------------------
# Remove child
# -----------------------------------------------------------------------------


class TestRemoveChild:
    def test_remove_middle_decl(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nconst A = 1\nconst B = 2\nconst C = 3\n"
        tree = backend.parse(src)
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        new = backend.remove_child(tree, symbols["B"])
        out = backend.serialize(new)
        assert "const B = 2" not in out
        assert "const A = 1" in out
        assert "const C = 3" in out

    def test_remove_function(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nfunc a() {}\nfunc b() {}\n"
        tree = backend.parse(src)
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        new = backend.remove_child(tree, symbols["a"])
        out = backend.serialize(new)
        assert "func a()" not in out
        assert "func b()" in out

    def test_remove_method(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\ntype R struct{}\n\nfunc (r R) A() {}\nfunc (r R) B() {}\n"
        tree = backend.parse(src)
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        new = backend.remove_child(tree, symbols["R/A"])
        out = backend.serialize(new)
        assert "func (r R) A()" not in out
        assert "func (r R) B()" in out

    def test_remove_missing_raises(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        fake = _GoSymbolRef(
            kind="function",
            name_path="ghost",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_remove_rejects_non_symbol(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        with pytest.raises(TypeError):
            backend.remove_child(tree, "not a ref")


# -----------------------------------------------------------------------------
# Empty source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: GoStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        assert backend.serialize(tree) == ""
        assert isinstance(tree, _GoTree)

    def test_empty_source_rejects_unknown_kind(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("function")


# -----------------------------------------------------------------------------
# Serialize type-checking
# -----------------------------------------------------------------------------


class TestSerialize:
    def test_serialize_tree(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        assert backend.serialize(tree) == "package p\n"

    def test_serialize_declaration(self, backend: GoStructuralLanguage) -> None:
        decl = backend.build_declaration("package", {"name": "p"}, [])
        assert backend.serialize(decl) == "package p\n"

    def test_serialize_rejects_bad_type(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.serialize("raw")


# -----------------------------------------------------------------------------
# Pattern matching
# -----------------------------------------------------------------------------


class TestPatterns:
    def test_compile_rejects_empty(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")
        with pytest.raises(PatternError):
            backend.compile_pattern("   \n  ")

    def test_unparseable_pattern_surfaces_on_find(self, backend: GoStructuralLanguage) -> None:
        # compile_pattern only validates non-empty; parse errors surface when
        # find_matches actually hands the pattern to the bridge, matching
        # the TypeScript backend's deferred-parse contract.
        tree = backend.parse("package p\n")
        pattern = backend.compile_pattern("!!! not go !!!")
        with pytest.raises(PatternError):
            list(backend.find_matches(tree, pattern))

    def test_find_exact_call_match(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nfunc main() {\n\tf(1)\n\tf(2)\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("f($arg)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 2
        args = sorted(m.bindings["arg"] for m in matches)
        assert args == ["1", "2"]

    def test_find_wildcard(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nfunc main() {\n\tfoo(1)\n\tbar(2)\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("$_(1)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1

    def test_find_no_match(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n\nvar x = 1\n")
        pattern = backend.compile_pattern("f($a)")
        matches = list(backend.find_matches(tree, pattern))
        assert matches == []

    def test_find_respects_repeated_capture(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nfunc main() {\n\teq(1, 1)\n\teq(1, 2)\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("eq($a, $a)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1
        assert matches[0].bindings["a"] == "1"

    def test_render_replacement(self, backend: GoStructuralLanguage) -> None:
        rep = backend.render_replacement("g($arg)", {"arg": "42"})
        assert rep.source == "g(42)"

    def test_render_replacement_missing_binding(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("g($missing)", {})

    def test_render_replacement_empty_rejected(self, backend: GoStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("", {})

    def test_apply_replacement_round_trip(self, backend: GoStructuralLanguage) -> None:
        src = "package p\n\nfunc main() {\n\tf(1)\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("f($arg)")
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement("g($arg)", match.bindings)
        tree2 = backend.apply_replacement(tree, match, replacement)
        assert "g(1)" in backend.serialize(tree2)
        assert "f(1)" not in backend.serialize(tree2)

    def test_find_matches_rejects_non_tree(self, backend: GoStructuralLanguage) -> None:
        pattern = backend.compile_pattern("f($a)")
        with pytest.raises(TypeError):
            list(backend.find_matches("not a tree", pattern))

    def test_find_matches_rejects_non_pattern(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        with pytest.raises(TypeError):
            list(backend.find_matches(tree, "not a pattern"))


# -----------------------------------------------------------------------------
# Registry exposure
# -----------------------------------------------------------------------------


class TestRegistryExposure:
    def test_go_registered(self) -> None:
        reg = default_structural_backend_registry()
        assert "go" in reg.registered_languages()

    def test_go_extension_routed(self) -> None:
        reg = default_structural_backend_registry()
        be = reg.for_relative_path("foo.go")
        assert be is not None
        assert be.language_key == "go"

    def test_go_extension_case_insensitive(self) -> None:
        reg = default_structural_backend_registry()
        be = reg.for_relative_path("Foo.GO")
        assert be is not None
        assert be.language_key == "go"


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


class TestErrorMapping:
    def test_remove_missing_symbol_raises(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        ghost = _GoSymbolRef(
            kind="function",
            name_path="does_not_exist",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, ghost)

    def test_apply_replacement_rejects_non_tree(self, backend: GoStructuralLanguage) -> None:
        rep = backend.render_replacement("x", {})
        fake_match = type("M", (), {"node": _GoSymbolRef("match", "", 0, 0, None), "bindings": {}, "symbol_path": None})
        with pytest.raises(TypeError):
            backend.apply_replacement("not a tree", fake_match, rep)

    def test_apply_replacement_rejects_non_declaration(self, backend: GoStructuralLanguage) -> None:
        tree = backend.parse("package p\n")
        fake_match = type("M", (), {"node": _GoSymbolRef("match", "", 0, 0, None), "bindings": {}, "symbol_path": None})
        with pytest.raises(TypeError):
            backend.apply_replacement(tree, fake_match, "not a decl")
