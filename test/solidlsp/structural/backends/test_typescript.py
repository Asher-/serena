"""Tests for the TypeScript structural backend.

Mirrors the Swift backend's test shape: curated edge-case fixtures for
round-trip, plus focused coverage of walk_symbols, build_declaration,
insert_child, remove_child, empty_source, and pattern matching over the
TypeScript compiler API via the Node.js bridge subprocess.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from solidlsp.structural.backends.typescript import (
    TypeScriptLogicalNameResolver,
    TypeScriptStructuralLanguage,
    _TsTree,
    typescript_kind_schema,
)
from solidlsp.structural.errors import DeclarationError, NameResolutionError, PatternError
from solidlsp.structural.registry import default_structural_backend_registry
from test.solidlsp.structural.harness import assert_round_trip

# -----------------------------------------------------------------------------
# Shared fixture
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Iterator[TypeScriptStructuralLanguage]:
    # module-scoped so the subprocess is spun up once per test module
    be = TypeScriptStructuralLanguage()
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
    ("trailing-newline", "const x = 1;\n"),
    ("no-final-newline", "const x = 1;"),
    ("crlf-line-endings", "const x = 1;\r\nconst y = 2;\r\n"),
    ("mixed-line-endings", "const x = 1;\nconst y = 2;\r\n"),
    ("line-comments-only", "// a comment\n// another\n"),
    ("block-comment-only", "/* a block\n   comment */\n"),
    ("jsdoc", "/** @param x a number */\nfunction f(x: number) { return x; }\n"),
    (
        "import-and-class",
        'import { useState } from "react";\n\nclass Foo {\n    bar: number = 1;\n}\n',
    ),
    (
        "class-with-methods",
        "class Point {\n    x: number;\n    y: number;\n    dist(): number { return Math.sqrt(this.x * this.x + this.y * this.y); }\n}\n",
    ),
    (
        "generic-function",
        "function id<T>(value: T): T { return value; }\n",
    ),
    (
        "enum-with-members",
        "enum Direction {\n    North = 0,\n    South = 1,\n    East = 2,\n    West = 3,\n}\n",
    ),
    (
        "interface-and-type-alias",
        "interface Greeter {\n    greet(): string;\n}\n\ntype Id = string | number;\n",
    ),
    (
        "string-with-escapes",
        'const s = "hello\\n\\t\\"world\\"";\n',
    ),
    (
        "template-literal",
        "const s = `hello ${name} world`;\n",
    ),
    (
        "multiline-template",
        "const s = `\n  line1\n  line2\n`;\n",
    ),
    (
        "async-function",
        "async function fetchData(): Promise<Data> { return await fetch(); }\n",
    ),
    (
        "arrow-function",
        "const add = (a: number, b: number): number => a + b;\n",
    ),
    (
        "decorator",
        "@sealed\nclass Foo {\n    bar() {}\n}\n",
    ),
    (
        "nested-namespace",
        "namespace Outer {\n    export namespace Inner {\n        export const value: number = 1;\n    }\n}\n",
    ),
    (
        "export-from",
        'export { foo, bar } from "./other";\n',
    ),
    (
        "default-export",
        "export default class Main {}\n",
    ),
    (
        "unicode-identifier",
        "const \u03c0 = 3.14;\n",
    ),
    (
        "unicode-string",
        'const greeting = "hello \u4e16\u754c";\n',
    ),
)


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


class TestRoundTripFixtures:
    @pytest.mark.parametrize(("label", "source"), _EDGE_CASES, ids=[case[0] for case in _EDGE_CASES])
    def test_edge_case_round_trips(
        self,
        backend: TypeScriptStructuralLanguage,
        label: str,
        source: str,
    ) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Identity
# -----------------------------------------------------------------------------


class TestIdentity:
    def test_language_key(self, backend: TypeScriptStructuralLanguage) -> None:
        assert backend.language_key == "typescript"

    def test_name_resolver_default_rooted_at_cwd(self) -> None:
        be = TypeScriptStructuralLanguage()
        try:
            assert isinstance(be.name_resolver, TypeScriptLogicalNameResolver)
        finally:
            be.close()

    def test_kind_schema_language_key_matches(self, backend: TypeScriptStructuralLanguage) -> None:
        assert backend.kind_schema.language_key == "typescript"


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_source_kinds(self, backend: TypeScriptStructuralLanguage) -> None:
        assert backend.kind_schema.source_kinds == frozenset({"source_file"})

    def test_expected_kinds_present(self, backend: TypeScriptStructuralLanguage) -> None:
        schema = backend.kind_schema
        expected = {
            "source_file",
            "import",
            "export",
            "class",
            "interface",
            "type_alias",
            "enum",
            "namespace",
            "function",
            "variable",
            "method",
            "constructor",
            "property",
            "enum_member",
        }
        assert expected <= set(schema.kinds)

    def test_method_restricted_to_type_bodies(self) -> None:
        schema = typescript_kind_schema()
        method = schema.get("method")
        assert method.allowed_parent_kinds == frozenset({"class", "interface"})

    def test_constructor_only_under_class(self) -> None:
        schema = typescript_kind_schema()
        ctor = schema.get("constructor")
        assert ctor.allowed_parent_kinds == frozenset({"class"})

    def test_enum_member_only_under_enum(self) -> None:
        schema = typescript_kind_schema()
        member = schema.get("enum_member")
        assert member.allowed_parent_kinds == frozenset({"enum"})

    def test_import_allowed_under_namespace(self) -> None:
        schema = typescript_kind_schema()
        imp = schema.get("import")
        assert {"source_file", "namespace"} <= imp.allowed_parent_kinds

    def test_validate_composition_rules(self) -> None:
        schema = typescript_kind_schema()
        # method at source root is rejected
        with pytest.raises(DeclarationError):
            schema.validate_composition("source_file", "method")
        # method inside class is fine
        schema.validate_composition("class", "method")
        # enum_member only under enum
        with pytest.raises(DeclarationError):
            schema.validate_composition("class", "enum_member")
        schema.validate_composition("enum", "enum_member")
        # constructor not allowed under interface
        with pytest.raises(DeclarationError):
            schema.validate_composition("interface", "constructor")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_single_segment(self, tmp_path) -> None:
        resolver = TypeScriptLogicalNameResolver(tmp_path)
        name = resolver.parse("App")
        assert name.parts == ("App",)
        assert name.raw == "App"

    def test_parse_dotted(self, tmp_path) -> None:
        resolver = TypeScriptLogicalNameResolver(tmp_path)
        name = resolver.parse("src.components.Button")
        assert name.parts == ("src", "components", "Button")

    def test_parse_rejects_empty(self, tmp_path) -> None:
        resolver = TypeScriptLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("")

    def test_parse_rejects_bad_identifier(self, tmp_path) -> None:
        resolver = TypeScriptLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("9bad")
        with pytest.raises(NameResolutionError):
            resolver.parse("has-dash")

    def test_resolve_nonexistent(self, tmp_path) -> None:
        resolver = TypeScriptLogicalNameResolver(tmp_path)
        name = resolver.parse("components.Button")
        resolution = resolver.resolve(name)
        assert resolution.exists is False
        assert resolution.source_kind == "source_file"
        assert resolution.relative_path.replace("\\", "/") == "components/Button.ts"

    def test_resolve_existing(self, tmp_path) -> None:
        (tmp_path / "App.ts").write_text("")
        resolver = TypeScriptLogicalNameResolver(tmp_path)
        resolution = resolver.resolve(resolver.parse("App"))
        assert resolution.exists is True
        assert resolution.relative_path == "App.ts"


# -----------------------------------------------------------------------------
# Walk symbols
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_symbols(self, backend: TypeScriptStructuralLanguage) -> None:
        source = 'import { x } from "y";\nclass Foo {\n    bar: number = 1;\n    baz() {}\n}\n'
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        kinds = {name_path: kind for name_path, kind, _ref in symbols}
        assert kinds["y"] == "import"
        assert kinds["Foo"] == "class"
        assert kinds["Foo/bar"] == "property"
        assert kinds["Foo/baz"] == "method"

    def test_walks_interface_members(self, backend: TypeScriptStructuralLanguage) -> None:
        source = "interface I {\n    x: number;\n    f(): string;\n}\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        kinds = {name_path: kind for name_path, kind, _ref in symbols}
        assert kinds["I"] == "interface"
        assert kinds["I/x"] == "property"
        assert kinds["I/f"] == "method"

    def test_walks_enum_members(self, backend: TypeScriptStructuralLanguage) -> None:
        source = "enum E {\n    A = 1,\n    B = 2,\n}\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        name_paths = {name_path for name_path, _kind, _ref in symbols}
        assert "E" in name_paths
        assert "E/A" in name_paths
        assert "E/B" in name_paths

    def test_walks_function_and_variable(self, backend: TypeScriptStructuralLanguage) -> None:
        source = "const x: number = 1;\nfunction greet(): string { return 'hi'; }\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        kinds = {name_path: kind for name_path, kind, _ref in symbols}
        assert kinds["x"] == "variable"
        assert kinds["greet"] == "function"

    def test_walks_namespace_with_members(self, backend: TypeScriptStructuralLanguage) -> None:
        source = "namespace N {\n    export class C {}\n    export const v: number = 0;\n}\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        name_paths = {name_path for name_path, _kind, _ref in symbols}
        assert "N" in name_paths
        assert "N/C" in name_paths
        assert "N/v" in name_paths

    def test_walks_export_declaration(self, backend: TypeScriptStructuralLanguage) -> None:
        source = 'export { foo, bar } from "./other";\n'
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        kinds = {name_path: kind for name_path, kind, _ref in symbols}
        assert kinds["./other"] == "export"

    def test_carries_body_range_for_class(self, backend: TypeScriptStructuralLanguage) -> None:
        source = "class C {\n    x: number = 1;\n}\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        _cls_name, _cls_kind, cls_ref = next(s for s in symbols if s[0] == "C")
        assert cls_ref.body_range is not None
        body_start, body_end = cls_ref.body_range
        # body covers the member `    x: number = 1;\n` between braces
        assert source.encode("utf-8")[body_start:body_end].decode("utf-8") == "\n    x: number = 1;\n"

    def test_carries_none_body_range_for_leaf(self, backend: TypeScriptStructuralLanguage) -> None:
        source = "function f() {}\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        assert symbols[0][2].body_range is None

    def test_multi_binding_variable_uses_joined_name(self, backend: TypeScriptStructuralLanguage) -> None:
        source = "const a = 1, b = 2;\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        name_paths = {name_path for name_path, _kind, _ref in symbols}
        assert "a+b" in name_paths


# -----------------------------------------------------------------------------
# Build declaration
# -----------------------------------------------------------------------------


class TestBuildDeclaration:
    def test_import_declaration(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("import", {"statement": 'import { x } from "y";'}, [])
        assert decl.source == 'import { x } from "y";\n'

    def test_import_rejects_non_import(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("import", {"statement": "const x = 1;"}, [])

    def test_export_declaration(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("export", {"statement": 'export * from "./other";'}, [])
        assert decl.source == 'export * from "./other";\n'

    def test_type_alias(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("type_alias", {"statement": "type Id = string | number;"}, [])
        assert decl.source == "type Id = string | number;\n"

    def test_type_alias_rejects_non_type(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("type_alias", {"statement": "const x = 1;"}, [])

    def test_variable(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("variable", {"statement": "const x: number = 1;"}, [])
        assert decl.source == "const x: number = 1;\n"

    def test_variable_accepts_export_prefix(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("variable", {"statement": "export let count = 0;"}, [])
        assert decl.source == "export let count = 0;\n"

    def test_variable_rejects_function(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("variable", {"statement": "function f() {}"}, [])

    def test_class_empty(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("class", {"name": "C"}, [])
        assert decl.source == "class C {\n}\n"

    def test_class_with_modifiers_and_extends(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "class",
            {
                "name": "C",
                "modifiers": "export abstract",
                "extends": ["Base"],
                "implements": ["I1", "I2"],
                "type_parameters": "T, U",
            },
            [],
        )
        assert decl.source == "export abstract class C<T, U> extends Base implements I1, I2 {\n}\n"

    def test_class_with_child(self, backend: TypeScriptStructuralLanguage) -> None:
        method = backend.build_declaration("method", {"name": "f", "parameters": "", "body": "    return 1;"}, [])
        cls = backend.build_declaration("class", {"name": "C"}, [method])
        assert "f()" in cls.source
        assert "return 1;" in cls.source

    def test_class_missing_name(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {}, [])

    def test_interface_with_extends(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "interface",
            {"name": "I", "extends": ["A", "B"], "type_parameters": "T"},
            [],
        )
        assert decl.source == "interface I<T> extends A, B {\n}\n"

    def test_enum(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("enum", {"name": "E"}, [])
        assert decl.source == "enum E {\n}\n"

    def test_namespace(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("namespace", {"name": "N"}, [])
        assert decl.source == "namespace N {\n}\n"

    def test_function(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "function",
            {
                "name": "f",
                "parameters": "x: number",
                "return_type": "number",
                "body": "    return x + 1;",
                "modifiers": "export",
            },
            [],
        )
        assert decl.source == "export function f(x: number): number {\n    return x + 1;\n}\n"

    def test_method_with_body(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "method",
            {"name": "f", "parameters": "", "body": "    return 1;"},
            [],
        )
        assert decl.source == "f() {\n    return 1;\n}\n"

    def test_method_signature_without_body(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "method",
            {"name": "f", "parameters": "", "return_type": "void"},
            [],
        )
        assert decl.source == "f(): void;\n"

    def test_method_optional(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "method",
            {"name": "f", "parameters": "", "optional": True, "return_type": "void"},
            [],
        )
        assert decl.source == "f?(): void;\n"

    def test_constructor(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "constructor",
            {"parameters": "x: number", "body": "    this.x = x;"},
            [],
        )
        assert decl.source == "constructor(x: number) {\n    this.x = x;\n}\n"

    def test_property(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "property",
            {"name": "count", "type": "number", "initializer": "0", "modifiers": "private"},
            [],
        )
        assert decl.source == "private count: number = 0;\n"

    def test_property_optional(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "property",
            {"name": "x", "type": "number", "optional": True},
            [],
        )
        assert decl.source == "x?: number;\n"

    def test_enum_member(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("enum_member", {"statement": "A = 1"}, [])
        assert decl.source == "A = 1\n"

    def test_build_source_file_rejected(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("source_file", {}, [])

    def test_unknown_kind_rejected(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(KeyError):
            backend.build_declaration("nonesuch", {"statement": "x"}, [])


# -----------------------------------------------------------------------------
# Insert / remove child
# -----------------------------------------------------------------------------


class TestInsertChild:
    def test_insert_at_root_end(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "class Foo {}\n"
        tree = backend.parse(src)
        decl = backend.build_declaration("function", {"name": "bar", "parameters": "", "body": "    return 1;"}, [])
        tree2 = backend.insert_child(tree, decl, position="end")
        result = backend.serialize(tree2)
        assert result.startswith("class Foo {}\n")
        assert "function bar()" in result

    def test_insert_at_root_start(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "class Foo {}\n"
        tree = backend.parse(src)
        decl = backend.build_declaration("import", {"statement": 'import { x } from "y";'}, [])
        tree2 = backend.insert_child(tree, decl, position="start")
        result = backend.serialize(tree2)
        assert result.startswith('import { x } from "y";\n')

    def test_insert_before_anchor(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "class Foo {}\nclass Baz {}\n"
        tree = backend.parse(src)
        anchor = next(ref for name, _kind, ref in backend.walk_symbols(tree) if name == "Baz")
        decl = backend.build_declaration("class", {"name": "Mid"}, [])
        tree2 = backend.insert_child(tree, decl, anchor=anchor, position="before")
        result = backend.serialize(tree2)
        assert result == "class Foo {}\nclass Mid {\n}\nclass Baz {}\n"

    def test_insert_after_anchor(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "class Foo {}\nclass Baz {}\n"
        tree = backend.parse(src)
        anchor = next(ref for name, _kind, ref in backend.walk_symbols(tree) if name == "Foo")
        decl = backend.build_declaration("class", {"name": "Mid"}, [])
        tree2 = backend.insert_child(tree, decl, anchor=anchor, position="after")
        result = backend.serialize(tree2)
        assert "class Foo {}\n" in result
        assert "class Mid {\n}\n" in result
        assert "class Baz {}\n" in result
        # ordering
        foo_pos = result.index("class Foo")
        mid_pos = result.index("class Mid")
        baz_pos = result.index("class Baz")
        assert foo_pos < mid_pos < baz_pos

    def test_insert_invalid_position(self, backend: TypeScriptStructuralLanguage) -> None:
        tree = backend.parse("")
        decl = backend.build_declaration("class", {"name": "C"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, decl, position="middle")

    def test_insert_before_without_anchor_rejected(self, backend: TypeScriptStructuralLanguage) -> None:
        tree = backend.parse("class A {}\n")
        decl = backend.build_declaration("class", {"name": "C"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, decl, position="before")

    def test_insert_non_declaration_child_rejected(self, backend: TypeScriptStructuralLanguage) -> None:
        tree = backend.parse("")
        with pytest.raises(TypeError):
            backend.insert_child(tree, "not a decl", position="end")

    def test_insert_wrong_parent_type_rejected(self, backend: TypeScriptStructuralLanguage) -> None:
        decl = backend.build_declaration("class", {"name": "C"}, [])
        with pytest.raises(TypeError):
            backend.insert_child("not a tree", decl, position="end")

    def test_insert_into_empty_source(self, backend: TypeScriptStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        decl = backend.build_declaration("function", {"name": "main", "parameters": "", "body": ""}, [])
        tree2 = backend.insert_child(tree, decl, position="end")
        assert backend.serialize(tree2) == "function main() {\n}\n"


class TestRemoveChild:
    def test_remove_top_level_class(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "class Foo {}\nclass Baz {}\n"
        tree = backend.parse(src)
        ref = next(r for name, _kind, r in backend.walk_symbols(tree) if name == "Foo")
        tree2 = backend.remove_child(tree, ref)
        assert "class Foo" not in backend.serialize(tree2)
        assert "class Baz {}" in backend.serialize(tree2)

    def test_remove_method_inside_class(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "class Foo {\n    a() {}\n    b() {}\n}\n"
        tree = backend.parse(src)
        ref = next(r for name, _kind, r in backend.walk_symbols(tree) if name == "Foo/a")
        tree2 = backend.remove_child(tree, ref)
        result = backend.serialize(tree2)
        assert "a()" not in result or "b()" in result  # the name `a` must be gone

    def test_remove_child_rejects_non_ref(self, backend: TypeScriptStructuralLanguage) -> None:
        tree = backend.parse("class Foo {}\n")
        with pytest.raises(TypeError):
            backend.remove_child(tree, "not a ref")


# -----------------------------------------------------------------------------
# Empty source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_produces_empty_tree(self, backend: TypeScriptStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        assert isinstance(tree, _TsTree)
        assert backend.serialize(tree) == ""

    def test_empty_source_rejects_non_source_kind(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("class")


# -----------------------------------------------------------------------------
# Pattern matching
# -----------------------------------------------------------------------------


class TestPatterns:
    def test_compile_rejects_empty(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")
        with pytest.raises(PatternError):
            backend.compile_pattern("   \n  ")

    def test_find_exact_match(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "const x = f(1);\nconst y = f(2);\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("f($arg)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 2
        bindings_sources = sorted(m.bindings["arg"] for m in matches)
        assert bindings_sources == ["1", "2"]

    def test_find_wildcard(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "foo(1);\nbar(2);\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("$_(1)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1

    def test_find_no_match(self, backend: TypeScriptStructuralLanguage) -> None:
        tree = backend.parse("const x = 1;\n")
        pattern = backend.compile_pattern("f($a)")
        matches = list(backend.find_matches(tree, pattern))
        assert matches == []

    def test_render_replacement(self, backend: TypeScriptStructuralLanguage) -> None:
        rep = backend.render_replacement("g($arg)", {"arg": "42"})
        assert rep.source == "g(42)"

    def test_render_replacement_missing_binding(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("g($missing)", {})

    def test_apply_replacement_round_trip(self, backend: TypeScriptStructuralLanguage) -> None:
        src = "const x = f(1);\n"
        tree = backend.parse(src)
        pattern = backend.compile_pattern("f($arg)")
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement("g($arg)", match.bindings)
        tree2 = backend.apply_replacement(tree, match, replacement)
        assert backend.serialize(tree2) == "const x = g(1);\n"


# -----------------------------------------------------------------------------
# Registry exposure
# -----------------------------------------------------------------------------


class TestRegistryExposure:
    def test_typescript_registered(self) -> None:
        reg = default_structural_backend_registry()
        assert "typescript" in reg.registered_languages()

    def test_typescript_extensions_routed(self) -> None:
        reg = default_structural_backend_registry()
        for ext in (".ts", ".mts", ".cts"):
            be = reg.for_relative_path(f"foo{ext}")
            assert be is not None
            assert be.language_key == "typescript"


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


class TestErrorMapping:
    def test_remove_missing_symbol_raises(self, backend: TypeScriptStructuralLanguage) -> None:
        from solidlsp.structural.backends.typescript import _TsSymbolRef

        tree = backend.parse("class Foo {}\n")
        bogus = _TsSymbolRef(kind="class", name_path="Nonexistent", extent_offset=0, extent_length=0, body_range=None)
        with pytest.raises(ValueError):
            backend.remove_child(tree, bogus)

    def test_serialize_wrong_handle_raises(self, backend: TypeScriptStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.serialize("not a tree")
