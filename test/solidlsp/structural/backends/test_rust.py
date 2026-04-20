"""Tests for the Rust structural backend.

Mirrors the Go backend's test shape: curated edge-case fixtures for
round-trip, plus focused coverage of walk_symbols, build_declaration,
insert_child, remove_child, empty_source, and pattern matching over the
Rust ``syn`` parser via the subprocess bridge.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path as _PathForTests

import pytest

from solidlsp.structural.backends.rust import (
    RustLogicalNameResolver,
    RustStructuralLanguage,
    _RustSymbolRef,
    _RustTree,
    rust_kind_schema,
)
from solidlsp.structural.errors import DeclarationError, NameResolutionError, PatternError
from solidlsp.structural.registry import default_structural_backend_registry
from test.solidlsp.structural.harness import assert_round_trip

# -----------------------------------------------------------------------------
# Shared fixture
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Iterator[RustStructuralLanguage]:
    # module-scoped so the subprocess is spun up once per test module
    be = RustStructuralLanguage()
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
    ("single-fn", "fn main() {}\n"),
    ("no-final-newline", "fn main() {}"),
    ("crlf-line-endings", "fn a() {}\r\nfn b() {}\r\n"),
    ("mixed-line-endings", "fn a() {}\nfn b() {}\r\n"),
    ("line-comments-only", "// comment 1\n// comment 2\n"),
    ("block-comment-only", "/* a block\n   comment */\n"),
    ("doc-comment-on-fn", "/// Does something.\nfn foo() {}\n"),
    ("inner-doc-comment", "//! crate-level\nfn main() {}\n"),
    ("attribute-on-fn", "#[inline]\nfn foo() {}\n"),
    ("cfg-attribute", '#[cfg(target_os = "linux")]\nfn only_linux() {}\n'),
    (
        "use-and-fn",
        "use std::io;\n\nfn main() {\n    let _ = io::stdin();\n}\n",
    ),
    (
        "grouped-use",
        "use std::{io, fs};\nuse foo::bar::{baz, qux as q};\n",
    ),
    (
        "struct-with-methods",
        "struct Point { x: i32, y: i32 }\n\nimpl Point {\n    fn norm(&self) -> i32 { self.x * self.x + self.y * self.y }\n}\n",
    ),
    (
        "generic-fn",
        "fn id<T>(v: T) -> T { v }\n",
    ),
    (
        "generic-struct",
        "struct Box<T> { v: T }\n",
    ),
    (
        "trait-decl",
        "trait Greeter {\n    fn greet(&self) -> String;\n}\n",
    ),
    (
        "trait-impl",
        'use std::fmt;\n\nstruct Point;\n\nimpl fmt::Display for Point {\n    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {\n        write!(f, "p")\n    }\n}\n',
    ),
    (
        "enum-with-variants",
        "enum Direction {\n    North,\n    South,\n    East,\n    West,\n}\n",
    ),
    (
        "enum-with-tuple-variants",
        "enum Shape {\n    Circle(f64),\n    Rect { w: f64, h: f64 },\n}\n",
    ),
    (
        "const-and-static",
        'const GREETING: &str = "hi";\nstatic COUNTER: i32 = 0;\n',
    ),
    (
        "type-alias",
        "type Id = u64;\ntype Callback<T> = fn(T) -> T;\n",
    ),
    (
        "string-with-escapes",
        'fn main() { let s = "hello\\n\\t\\"world\\""; }\n',
    ),
    (
        "raw-string",
        'fn main() { let r = r"C:\\path\\to\\file"; }\n',
    ),
    (
        "byte-and-char",
        "fn main() {\n    let b = b'A';\n    let c = 'A';\n    let r = '\\n';\n}\n",
    ),
    (
        "match-expr",
        'fn f(x: i32) -> &\'static str {\n    match x {\n        0 => "zero",\n        _ => "other",\n    }\n}\n',
    ),
    (
        "lifetime-generics",
        "struct Wrap<'a, T: 'a> { inner: &'a T }\n",
    ),
    (
        "where-clause",
        "fn pair<T, U>(a: T, b: U) -> (T, U)\nwhere\n    T: Clone,\n    U: Clone,\n{\n    (a, b)\n}\n",
    ),
    (
        "inline-mod",
        "mod inner {\n    pub fn helper() {}\n}\n",
    ),
    (
        "mod-decl",
        "mod other;\npub mod public_mod;\n",
    ),
    (
        "async-fn",
        "async fn fetch() -> u32 { 42 }\n",
    ),
    (
        "unsafe-fn",
        "unsafe fn raw() {}\n",
    ),
    (
        "pub-crate-fn",
        "pub(crate) fn internal() {}\n",
    ),
    (
        "macro-invocation-not-walked",
        'fn main() { println!("hi"); }\n',
    ),
)


class TestRoundTripFixtures:
    @pytest.mark.parametrize(("label", "source"), _EDGE_CASES, ids=[case[0] for case in _EDGE_CASES])
    def test_edge_case_round_trips(
        self,
        backend: RustStructuralLanguage,
        label: str,
        source: str,
    ) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Identity
# -----------------------------------------------------------------------------


class TestIdentity:
    def test_language_key(self, backend: RustStructuralLanguage) -> None:
        assert backend.language_key == "rust"

    def test_kind_schema_identity(self, backend: RustStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "rust"
        assert schema.source_kinds == frozenset({"source_file"})

    def test_name_resolver_is_rust_resolver(self, backend: RustStructuralLanguage) -> None:
        assert isinstance(backend.name_resolver, RustLogicalNameResolver)


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_expected_kinds_present(self) -> None:
        schema = rust_kind_schema()
        expected = {
            "source_file",
            "use",
            "mod",
            "const",
            "static",
            "type",
            "fn",
            "struct",
            "enum",
            "trait",
            "impl",
            "method",
        }
        assert set(schema.kinds) == expected

    def test_source_file_children(self) -> None:
        schema = rust_kind_schema()
        sf = schema.get("source_file")
        assert sf.allowed_parent_kinds == frozenset()
        # method is reachable via walk_symbols but is not a direct source_file child
        assert sf.allowed_child_kinds == frozenset({"use", "mod", "const", "static", "type", "fn", "struct", "enum", "trait", "impl"})

    def test_use_only_in_source_file(self) -> None:
        schema = rust_kind_schema()
        schema.validate_composition("source_file", "use")
        with pytest.raises(DeclarationError):
            schema.validate_composition("fn", "use")

    def test_fn_only_in_source_file(self) -> None:
        schema = rust_kind_schema()
        schema.validate_composition("source_file", "fn")
        with pytest.raises(DeclarationError):
            schema.validate_composition("impl", "fn")

    def test_impl_only_in_source_file(self) -> None:
        schema = rust_kind_schema()
        schema.validate_composition("source_file", "impl")
        with pytest.raises(DeclarationError):
            schema.validate_composition("struct", "impl")

    def test_method_not_insertable_in_v1(self) -> None:
        # v1 limitation: methods are walked/removable but cannot be inserted
        # anywhere. impl blocks do not expose body ranges, and source_file
        # does not accept raw methods as direct children.
        schema = rust_kind_schema()
        with pytest.raises(DeclarationError):
            schema.validate_composition("source_file", "method")
        with pytest.raises(DeclarationError):
            schema.validate_composition("impl", "method")

    def test_all_top_level_kinds_are_leaves(self) -> None:
        schema = rust_kind_schema()
        for name in (
            "use",
            "mod",
            "const",
            "static",
            "type",
            "fn",
            "struct",
            "enum",
            "trait",
            "impl",
            "method",
        ):
            assert schema.get(name).allowed_child_kinds == frozenset()

    def test_unknown_kind_raises(self) -> None:
        schema = rust_kind_schema()
        with pytest.raises(KeyError):
            schema.get("class")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_and_resolve_single(self, tmp_path: _PathForTests) -> None:
        resolver = RustLogicalNameResolver(tmp_path)
        name = resolver.parse("mod_x")
        assert name.raw == "mod_x"
        assert name.parts == ("mod_x",)
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "mod_x.rs"
        assert resolution.source_kind == "source_file"
        assert resolution.exists is False

    def test_parse_and_resolve_dotted(self, tmp_path: _PathForTests) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "util.rs").write_text("pub fn helper() {}\n")
        resolver = RustLogicalNameResolver(tmp_path)
        resolution = resolver.resolve(resolver.parse("pkg.util"))
        assert resolution.relative_path == "pkg/util.rs"
        assert resolution.exists is True

    def test_empty_name_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = RustLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("")

    def test_invalid_segment_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = RustLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("pkg.3util")

    def test_invalid_character_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = RustLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("pkg.util-helper")


# -----------------------------------------------------------------------------
# Root kind
# -----------------------------------------------------------------------------


class TestRootKind:
    def test_root_kind_is_source_file(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("")
        assert backend.root_kind(tree) == "source_file"

    def test_root_kind_rejects_non_tree(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.root_kind("not a tree")


# -----------------------------------------------------------------------------
# Walk symbols
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_fn(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn top_level() {}\n")
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds == {"top_level": "fn"}

    def test_walks_use_simple(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("use std::io;\nuse foo::bar;\n")
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["std::io"] == "use"
        assert kinds["foo::bar"] == "use"

    def test_walks_grouped_use(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("use std::{io, fs::File};\n")
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        # group expands to joined leaf paths
        assert kinds["std::io+std::fs::File"] == "use"

    def test_walks_aliased_use_ignores_alias(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("use foo::bar as baz;\n")
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["foo::bar"] == "use"

    def test_walks_const_static_type(self, backend: RustStructuralLanguage) -> None:
        src = 'const A: i32 = 1;\nstatic B: &str = "x";\ntype Id = u64;\n'
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["A"] == "const"
        assert kinds["B"] == "static"
        assert kinds["Id"] == "type"

    def test_walks_struct_enum_trait(self, backend: RustStructuralLanguage) -> None:
        src = "struct S;\nenum E { A, B }\ntrait T {}\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["S"] == "struct"
        assert kinds["E"] == "enum"
        assert kinds["T"] == "trait"

    def test_walks_mod(self, backend: RustStructuralLanguage) -> None:
        src = "mod external;\nmod inline { fn inner() {} }\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        assert kinds["external"] == "mod"
        assert kinds["inline"] == "mod"

    def test_walks_inherent_impl_methods(self, backend: RustStructuralLanguage) -> None:
        src = "struct Point;\nimpl Point {\n    fn x(&self) -> i32 { 0 }\n    fn set_x(&mut self, _x: i32) {}\n}\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        # struct and impl have distinct name_paths by design (no collision)
        assert kinds["Point"] == "struct"
        assert kinds["impl Point"] == "impl"
        impl_methods = [k for k in kinds if k.startswith("impl Point/")]
        assert set(impl_methods) == {"impl Point/x", "impl Point/set_x"}
        assert kinds["impl Point/x"] == "method"

    def test_walks_trait_impl_uses_angle_syntax(self, backend: RustStructuralLanguage) -> None:
        src = (
            "use std::fmt;\n\n"
            "struct Point;\n\n"
            "impl fmt::Display for Point {\n"
            "    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { Ok(()) }\n"
            "}\n"
        )
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        # trait impls use "impl Trait for Type" to avoid colliding with
        # either the inherent impl or the type's own name_path
        assert kinds["impl fmt::Display for Point"] == "impl"
        assert kinds["impl fmt::Display for Point/fmt"] == "method"

    def test_walks_generic_impl(self, backend: RustStructuralLanguage) -> None:
        src = "struct Box<T> { v: T }\nimpl<T> Box<T> {\n    fn new(v: T) -> Self { Box { v } }\n}\n"
        tree = backend.parse(src)
        kinds = {np: kind for np, kind, _ in backend.walk_symbols(tree)}
        # generic impl: name_path is "impl <self_ty>" where self_ty retains
        # the generic parameter list from the source
        impl_entries = [np for np, k in kinds.items() if k == "impl"]
        assert len(impl_entries) == 1
        assert impl_entries[0].startswith("impl Box")

    def test_walk_extents_point_into_source(self, backend: RustStructuralLanguage) -> None:
        src = "fn foo() {}\n"
        tree = backend.parse(src)
        ref = next(r for np, _, r in backend.walk_symbols(tree) if np == "foo")
        assert src[ref.extent_offset : ref.extent_offset + ref.extent_length].startswith("fn foo()")

    def test_walk_body_range_is_none_v1(self, backend: RustStructuralLanguage) -> None:
        src = "struct Foo { x: i32 }\n"
        tree = backend.parse(src)
        refs = [r for _, _, r in backend.walk_symbols(tree)]
        for r in refs:
            assert r.body_range is None

    def test_walk_symbols_empty_source(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("")
        assert list(backend.walk_symbols(tree)) == []

    def test_walk_symbols_rejects_non_tree(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            list(backend.walk_symbols("not a tree"))


# -----------------------------------------------------------------------------
# Declaration building
# -----------------------------------------------------------------------------


class TestBuildDeclaration:
    def test_build_use(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("use", {"statement": "use std::io;"}, [])
        assert decl.source == "use std::io;\n"
        assert decl.kind == "use"

    def test_build_use_with_visibility(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("use", {"statement": "pub use std::io;"}, [])
        assert decl.source == "pub use std::io;\n"

    def test_build_use_requires_use_keyword(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("use", {"statement": "std::io;"}, [])

    def test_build_mod(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("mod", {"statement": "mod inner;"}, [])
        assert decl.source == "mod inner;\n"

    def test_build_const(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("const", {"statement": "const X: i32 = 1;"}, [])
        assert decl.source == "const X: i32 = 1;\n"

    def test_build_static(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("static", {"statement": 'static G: &str = "hi";'}, [])
        assert decl.source == 'static G: &str = "hi";\n'

    def test_build_type(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("type", {"statement": "type Id = u64;"}, [])
        assert decl.source == "type Id = u64;\n"

    def test_build_struct(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("struct", {"statement": "struct Point { x: i32, y: i32 }"}, [])
        assert decl.source == "struct Point { x: i32, y: i32 }\n"

    def test_build_enum(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("enum", {"statement": "enum E { A, B }"}, [])
        assert decl.source == "enum E { A, B }\n"

    def test_build_trait(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("trait", {"statement": "trait T { fn m(&self); }"}, [])
        assert decl.source == "trait T { fn m(&self); }\n"

    def test_build_fn_minimal(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("fn", {"name": "foo", "body": ""}, [])
        assert decl.source == "fn foo() {\n}\n"

    def test_build_fn_with_params_and_return(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "fn",
            {
                "name": "add",
                "parameters": "a: i32, b: i32",
                "return_type": "i32",
                "body": "    a + b\n",
            },
            [],
        )
        assert decl.source == "fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"

    def test_build_fn_with_generics(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "fn",
            {
                "name": "id",
                "generics": "T",
                "parameters": "v: T",
                "return_type": "T",
                "body": "    v\n",
            },
            [],
        )
        assert decl.source == "fn id<T>(v: T) -> T {\n    v\n}\n"

    def test_build_fn_with_visibility(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "fn",
            {"name": "public", "visibility": "pub", "body": ""},
            [],
        )
        assert decl.source == "pub fn public() {\n}\n"

    def test_build_fn_with_modifiers(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "fn",
            {"name": "raw", "modifiers": "unsafe", "body": ""},
            [],
        )
        assert decl.source == "unsafe fn raw() {\n}\n"

    def test_build_fn_with_where(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "fn",
            {
                "name": "f",
                "generics": "T",
                "parameters": "x: T",
                "where_clause": "T: Clone",
                "body": "",
            },
            [],
        )
        assert "where" in decl.source
        assert "T: Clone" in decl.source

    def test_build_method_uses_fn_keyword(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "method",
            {
                "name": "norm",
                "parameters": "&self",
                "return_type": "i32",
                "body": "        self.x\n",
            },
            [],
        )
        assert decl.source.startswith("fn norm(&self) -> i32 {")

    def test_build_impl_inherent(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "impl",
            {"self_type": "Point", "body": "    fn x() {}\n"},
            [],
        )
        assert decl.source == "impl Point {\n    fn x() {}\n}\n"

    def test_build_impl_trait(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "impl",
            {"self_type": "Point", "trait": "Display", "body": ""},
            [],
        )
        assert decl.source == "impl Display for Point {\n}\n"

    def test_build_impl_generic(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "impl",
            {"self_type": "Box<T>", "generics": "T", "body": ""},
            [],
        )
        assert decl.source == "impl<T> Box<T> {\n}\n"

    def test_build_declaration_missing_required_raises(
        self,
        backend: RustStructuralLanguage,
    ) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("fn", {}, [])

    def test_build_declaration_wrong_type_raises(
        self,
        backend: RustStructuralLanguage,
    ) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("fn", {"name": 42, "body": ""}, [])

    def test_build_source_file_rejected(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("source_file", {}, [])

    def test_build_unknown_kind_raises(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(KeyError):
            backend.build_declaration("interface", {"name": "I"}, [])

    def test_build_rejects_foreign_child(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("fn", {"name": "f", "body": ""}, ["not a decl"])


# -----------------------------------------------------------------------------
# Insert child
# -----------------------------------------------------------------------------


class TestInsertChild:
    def test_insert_end_into_empty(self, backend: RustStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        fn = backend.build_declaration("fn", {"name": "main", "body": ""}, [])
        new = backend.insert_child(tree, fn, anchor=None, position="end")
        assert backend.serialize(new) == "fn main() {\n}\n"

    def test_insert_end_into_file(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        decl = backend.build_declaration("fn", {"name": "b", "body": ""}, [])
        new = backend.insert_child(tree, decl, anchor=None, position="end")
        assert backend.serialize(new) == "fn a() {}\nfn b() {\n}\n"

    def test_insert_before_anchor(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("const A: i32 = 1;\nconst C: i32 = 3;\n")
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        b_decl = backend.build_declaration("const", {"statement": "const B: i32 = 2;"}, [])
        new = backend.insert_child(tree, b_decl, anchor=symbols["C"], position="before")
        out = backend.serialize(new)
        assert "const A: i32 = 1;\nconst B: i32 = 2;\nconst C: i32 = 3;\n" in out

    def test_insert_after_anchor(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("const A: i32 = 1;\nconst C: i32 = 3;\n")
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        b_decl = backend.build_declaration("const", {"statement": "const B: i32 = 2;"}, [])
        new = backend.insert_child(tree, b_decl, anchor=symbols["A"], position="after")
        out = backend.serialize(new)
        assert "const A: i32 = 1;\nconst B: i32 = 2;\nconst C: i32 = 3;\n" in out

    def test_insert_start_prefixes_source(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        decl = backend.build_declaration("const", {"statement": "const X: i32 = 1;"}, [])
        new = backend.insert_child(tree, decl, anchor=None, position="start")
        assert backend.serialize(new).startswith("const X: i32 = 1;\n")

    def test_insert_invalid_position_raises(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        decl = backend.build_declaration("const", {"statement": "const X: i32 = 1;"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, decl, position="between")

    def test_insert_before_without_anchor_raises(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        decl = backend.build_declaration("const", {"statement": "const X: i32 = 1;"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, decl, anchor=None, position="before")

    def test_insert_rejects_non_declaration_child(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        with pytest.raises(TypeError):
            backend.insert_child(tree, "raw string", anchor=None, position="end")

    def test_insert_rejects_non_tree_parent(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("const", {"statement": "const X: i32 = 1;"}, [])
        with pytest.raises(TypeError):
            backend.insert_child("not a tree", decl, anchor=None, position="end")

    def test_insert_with_symbol_ref_parent_raises(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn foo() {}\n")
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        decl = backend.build_declaration("const", {"statement": "const X: i32 = 1;"}, [])
        with pytest.raises(TypeError):
            backend.insert_child(symbols["foo"], decl, anchor=None, position="end")


# -----------------------------------------------------------------------------
# Remove child
# -----------------------------------------------------------------------------


class TestRemoveChild:
    def test_remove_middle_decl(self, backend: RustStructuralLanguage) -> None:
        src = "const A: i32 = 1;\nconst B: i32 = 2;\nconst C: i32 = 3;\n"
        tree = backend.parse(src)
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        new = backend.remove_child(tree, symbols["B"])
        out = backend.serialize(new)
        assert "const B" not in out
        assert "const A" in out
        assert "const C" in out

    def test_remove_fn(self, backend: RustStructuralLanguage) -> None:
        src = "fn a() {}\nfn b() {}\n"
        tree = backend.parse(src)
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        new = backend.remove_child(tree, symbols["a"])
        out = backend.serialize(new)
        assert "fn a()" not in out
        assert "fn b()" in out

    def test_remove_method_from_impl(self, backend: RustStructuralLanguage) -> None:
        src = "struct R;\nimpl R {\n    fn a(&self) {}\n    fn b(&self) {}\n}\n"
        tree = backend.parse(src)
        symbols = {np: ref for np, _, ref in backend.walk_symbols(tree)}
        new = backend.remove_child(tree, symbols["impl R/a"])
        out = backend.serialize(new)
        assert "fn a(&self)" not in out
        assert "fn b(&self)" in out

    def test_remove_missing_raises(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        fake = _RustSymbolRef(
            kind="fn",
            name_path="ghost",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_remove_rejects_non_symbol(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        with pytest.raises(TypeError):
            backend.remove_child(tree, "not a ref")


# -----------------------------------------------------------------------------
# Empty source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: RustStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        assert backend.serialize(tree) == ""
        assert isinstance(tree, _RustTree)

    def test_empty_source_rejects_unknown_kind(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("fn")


# -----------------------------------------------------------------------------
# Serialize type-checking
# -----------------------------------------------------------------------------


class TestSerialize:
    def test_serialize_tree(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        assert backend.serialize(tree) == "fn a() {}\n"

    def test_serialize_declaration(self, backend: RustStructuralLanguage) -> None:
        decl = backend.build_declaration("fn", {"name": "f", "body": ""}, [])
        assert backend.serialize(decl) == "fn f() {\n}\n"

    def test_serialize_rejects_bad_type(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.serialize("raw")


# -----------------------------------------------------------------------------
# Pattern matching
# -----------------------------------------------------------------------------


class TestPatterns:
    def test_compile_rejects_empty(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")
        with pytest.raises(PatternError):
            backend.compile_pattern("   \n  ")

    def test_unparseable_pattern_surfaces_on_find(self, backend: RustStructuralLanguage) -> None:
        # compile_pattern only validates non-empty; parse errors surface when
        # find_matches actually hands the pattern to the bridge, matching
        # the Go / TypeScript backends' deferred-parse contract.
        tree = backend.parse("fn a() {}\n")
        pattern = backend.compile_pattern("!!! not rust !!!")
        with pytest.raises(PatternError):
            list(backend.find_matches(tree, pattern))

    def test_find_exact_call_match(self, backend: RustStructuralLanguage) -> None:
        src = "fn main() {\n    f(1);\n    f(2);\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("f($arg)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 2
        args = sorted(m.bindings["arg"] for m in matches)
        assert args == ["1", "2"]

    def test_find_wildcard(self, backend: RustStructuralLanguage) -> None:
        src = "fn main() {\n    foo(1);\n    bar(2);\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("$_(1)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1

    def test_find_no_match(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("const X: i32 = 1;\n")
        pattern = backend.compile_pattern("f($a)")
        matches = list(backend.find_matches(tree, pattern))
        assert matches == []

    def test_find_respects_repeated_capture(self, backend: RustStructuralLanguage) -> None:
        src = "fn main() {\n    eq(1, 1);\n    eq(1, 2);\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("eq($a, $a)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1
        assert matches[0].bindings["a"] == "1"

    def test_render_replacement(self, backend: RustStructuralLanguage) -> None:
        rep = backend.render_replacement("g($arg)", {"arg": "42"})
        assert rep.source == "g(42)"

    def test_render_replacement_missing_binding(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("g($missing)", {})

    def test_render_replacement_empty_rejected(self, backend: RustStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("", {})

    def test_apply_replacement_round_trip(self, backend: RustStructuralLanguage) -> None:
        src = "fn main() {\n    f(1);\n}\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("f($arg)")
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement("g($arg)", match.bindings)
        tree2 = backend.apply_replacement(tree, match, replacement)
        assert "g(1)" in backend.serialize(tree2)
        assert "f(1)" not in backend.serialize(tree2)

    def test_find_matches_rejects_non_tree(self, backend: RustStructuralLanguage) -> None:
        pattern = backend.compile_pattern("f($a)")
        with pytest.raises(TypeError):
            list(backend.find_matches("not a tree", pattern))

    def test_find_matches_rejects_non_pattern(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        with pytest.raises(TypeError):
            list(backend.find_matches(tree, "not a pattern"))


# -----------------------------------------------------------------------------
# Registry exposure
# -----------------------------------------------------------------------------


class TestRegistryExposure:
    def test_rust_registered(self) -> None:
        reg = default_structural_backend_registry()
        assert "rust" in reg.registered_languages()

    def test_rust_extension_routed(self) -> None:
        reg = default_structural_backend_registry()
        be = reg.for_relative_path("foo.rs")
        assert be is not None
        assert be.language_key == "rust"

    def test_rust_extension_case_insensitive(self) -> None:
        reg = default_structural_backend_registry()
        be = reg.for_relative_path("Foo.RS")
        assert be is not None
        assert be.language_key == "rust"


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


class TestErrorMapping:
    def test_remove_missing_symbol_raises(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        ghost = _RustSymbolRef(
            kind="fn",
            name_path="does_not_exist",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, ghost)

    def test_apply_replacement_rejects_non_tree(self, backend: RustStructuralLanguage) -> None:
        rep = backend.render_replacement("x", {})
        fake_match = type("M", (), {"node": _RustSymbolRef("match", "", 0, 0, None), "bindings": {}, "symbol_path": None})
        with pytest.raises(TypeError):
            backend.apply_replacement("not a tree", fake_match, rep)

    def test_apply_replacement_rejects_non_declaration(self, backend: RustStructuralLanguage) -> None:
        tree = backend.parse("fn a() {}\n")
        fake_match = type("M", (), {"node": _RustSymbolRef("match", "", 0, 0, None), "bindings": {}, "symbol_path": None})
        with pytest.raises(TypeError):
            backend.apply_replacement(tree, fake_match, "not a decl")
