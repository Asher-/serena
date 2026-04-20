"""Tests for the Ruby structural backend.

Mirrors the Java backend's test shape: curated edge-case fixtures for
round-trip, plus focused coverage of walk_symbols, build_declaration,
insert_child, remove_child, empty_source, and pattern matching over the
Ruby ``Prism`` parser via the subprocess bridge.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path as _PathForTests

import pytest

from solidlsp.structural.backends.ruby import (
    RubyLogicalNameResolver,
    RubyStructuralLanguage,
    _RubySymbolRef,
    ruby_kind_schema,
)
from solidlsp.structural.errors import DeclarationError, NameResolutionError, PatternError
from solidlsp.structural.registry import default_structural_backend_registry
from test.solidlsp.structural.harness import assert_round_trip

# -----------------------------------------------------------------------------
# Shared fixture
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backend() -> Iterator[RubyStructuralLanguage]:
    be = RubyStructuralLanguage()
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
    ("single-class", "class Hello\nend\n"),
    ("no-final-newline", "class Hello\nend"),
    ("crlf-line-endings", "class A\r\nend\r\nclass B\r\nend\r\n"),
    ("mixed-line-endings", "class A\nend\nclass B\r\nend\r\n"),
    ("line-comments-only", "# comment 1\n# comment 2\n"),
    ("block-comment-only", "=begin\na block\ncomment\n=end\n"),
    ("shebang-only", "#!/usr/bin/env ruby\n"),
    ("frozen-string-literal-magic", "# frozen_string_literal: true\nclass C\nend\n"),
    ("single-module", "module M\nend\n"),
    ("require-simple", "require 'json'\n"),
    ("require-relative", "require_relative './util'\n"),
    ("require-double-quoted", 'require "json"\n'),
    ("multiple-requires", "require 'json'\nrequire 'set'\nrequire_relative './util'\n"),
    (
        "class-with-method",
        "class C\n  def foo\n  end\nend\n",
    ),
    (
        "class-with-method-with-body",
        "class C\n  def foo\n    42\n  end\nend\n",
    ),
    (
        "class-with-method-with-params",
        "class C\n  def add(a, b)\n    a + b\n  end\nend\n",
    ),
    (
        "class-with-singleton-method",
        "class C\n  def self.build\n    new\n  end\nend\n",
    ),
    (
        "class-with-mixed-methods",
        "class C\n  def instance_m\n  end\n  def self.class_m\n  end\nend\n",
    ),
    (
        "class-with-constant",
        "class C\n  MAX = 10\nend\n",
    ),
    (
        "class-with-alias",
        "class C\n  def foo\n  end\n  alias bar foo\nend\n",
    ),
    (
        "module-with-method",
        "module M\n  def mixed_in\n  end\nend\n",
    ),
    (
        "nested-class",
        "class Outer\n  class Inner\n    def f\n    end\n  end\nend\n",
    ),
    (
        "class-inheritance",
        "class Child < Parent\nend\n",
    ),
    (
        "path-form-class",
        "class Outer::Inner\nend\n",
    ),
    (
        "path-form-constant",
        "Foo::BAR = 1\n",
    ),
    (
        "top-level-constant",
        "MAX_WIDTH = 80\n",
    ),
    (
        "multiple-top-level",
        "class A\nend\nclass B\nend\n",
    ),
    (
        "method-with-default-arg",
        "def greet(name = 'world')\n  name\nend\n",
    ),
    (
        "method-with-splat",
        "def foo(*args, **kwargs)\n  args\nend\n",
    ),
    (
        "method-with-block-arg",
        "def each(&blk)\n  blk.call\nend\n",
    ),
    (
        "predicate-method",
        "class C\n  def empty?\n    true\n  end\nend\n",
    ),
    (
        "bang-method",
        "class C\n  def reset!\n  end\nend\n",
    ),
    (
        "setter-method",
        "class C\n  def value=(v)\n    @v = v\n  end\nend\n",
    ),
    (
        "unicode-identifier",
        "class Caf\u00e9\n  def \u00e9t\u00e9\n    'summer'\n  end\nend\n",
    ),
    (
        "unicode-in-string",
        'class C\n  GREETING = "caf\u00e9"\nend\n',
    ),
    (
        "heredoc",
        "class C\n  def msg\n    <<~END\n      hello\n    END\n  end\nend\n",
    ),
    (
        "trailing-whitespace-lines",
        "class C\nend\n   \n  \n",
    ),
    (
        "ws-leading",
        "    \nclass C\nend\n",
    ),
)


class TestRoundTripFixtures:
    @pytest.mark.parametrize("label,source", _EDGE_CASES, ids=[c[0] for c in _EDGE_CASES])
    def test_edge_case_round_trips(self, backend: RubyStructuralLanguage, label: str, source: str) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Identity
# -----------------------------------------------------------------------------


class TestIdentity:
    def test_language_key(self, backend: RubyStructuralLanguage) -> None:
        assert backend.language_key == "ruby"

    def test_kind_schema_identity(self, backend: RubyStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "ruby"
        assert "source_file" in schema.source_kinds

    def test_name_resolver_is_ruby_resolver(self, backend: RubyStructuralLanguage, tmp_path: _PathForTests) -> None:
        be = RubyStructuralLanguage(name_resolver=RubyLogicalNameResolver(tmp_path))
        assert isinstance(be.name_resolver, RubyLogicalNameResolver)
        be.close()


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_expected_kinds_present(self) -> None:
        schema = ruby_kind_schema()
        expected = {
            "source_file",
            "class",
            "module",
            "method",
            "singleton_method",
            "constant",
            "require",
            "alias",
        }
        assert set(schema.kinds) == expected

    def test_source_file_children(self) -> None:
        schema = ruby_kind_schema()
        sf = schema.get("source_file")
        assert sf.allowed_child_kinds == frozenset({"class", "module", "method", "constant", "require", "alias"})

    def test_class_only_in_source_file(self) -> None:
        schema = ruby_kind_schema()
        assert schema.get("class").allowed_parent_kinds == frozenset({"source_file"})

    def test_module_only_in_source_file(self) -> None:
        schema = ruby_kind_schema()
        assert schema.get("module").allowed_parent_kinds == frozenset({"source_file"})

    def test_method_top_level_only(self) -> None:
        schema = ruby_kind_schema()
        assert schema.get("method").allowed_parent_kinds == frozenset({"source_file"})

    def test_singleton_method_not_insertable(self) -> None:
        schema = ruby_kind_schema()
        # singleton_method is walk-only: no kind accepts it as a child
        assert schema.get("singleton_method").allowed_parent_kinds == frozenset()

    def test_constant_top_level_only(self) -> None:
        schema = ruby_kind_schema()
        assert schema.get("constant").allowed_parent_kinds == frozenset({"source_file"})

    def test_require_top_level_only(self) -> None:
        schema = ruby_kind_schema()
        assert schema.get("require").allowed_parent_kinds == frozenset({"source_file"})

    def test_alias_top_level_only(self) -> None:
        schema = ruby_kind_schema()
        assert schema.get("alias").allowed_parent_kinds == frozenset({"source_file"})

    def test_all_kinds_are_leaves(self) -> None:
        schema = ruby_kind_schema()
        for kind_name in ("class", "module", "method", "singleton_method", "constant", "require", "alias"):
            assert schema.get(kind_name).allowed_child_kinds == frozenset(), kind_name

    def test_unknown_kind_raises(self) -> None:
        schema = ruby_kind_schema()
        with pytest.raises(KeyError):
            schema.get("does-not-exist")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_and_resolve_single(self, tmp_path: _PathForTests) -> None:
        resolver = RubyLogicalNameResolver(tmp_path)
        name = resolver.parse("foo")
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "foo.rb"
        assert resolution.source_kind == "source_file"
        assert resolution.exists is False

    def test_parse_and_resolve_dotted(self, tmp_path: _PathForTests) -> None:
        resolver = RubyLogicalNameResolver(tmp_path)
        (tmp_path / "lib" / "app").mkdir(parents=True)
        (tmp_path / "lib" / "app" / "foo.rb").write_text("class Foo\nend\n")
        name = resolver.parse("lib.app.foo")
        resolution = resolver.resolve(name)
        assert resolution.relative_path == "lib/app/foo.rb"
        assert resolution.exists is True

    def test_empty_name_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = RubyLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("")

    def test_invalid_segment_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = RubyLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("foo..bar")

    def test_invalid_character_rejected(self, tmp_path: _PathForTests) -> None:
        resolver = RubyLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("foo-bar")

    def test_dash_rejected_in_segment(self, tmp_path: _PathForTests) -> None:
        resolver = RubyLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse("foo.bar-baz")


# -----------------------------------------------------------------------------
# Root kind
# -----------------------------------------------------------------------------


class TestRootKind:
    def test_root_kind_is_source_file(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class C\nend\n")
        assert backend.root_kind(tree) == "source_file"

    def test_root_kind_rejects_non_tree(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.root_kind("not a tree")


# -----------------------------------------------------------------------------
# walk_symbols
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_class(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class Foo\nend\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Foo", "class") in names

    def test_walks_top_level_module(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("module M\nend\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("M", "module") in names

    def test_walks_top_level_method(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("def hello\nend\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("hello", "method") in names

    def test_walks_require(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("require 'json'\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("json", "require") in names

    def test_walks_require_relative(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("require_relative './util'\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("./util", "require") in names

    def test_walks_top_level_constant(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("BAZ = 1\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("BAZ", "constant") in names

    def test_walks_path_constant(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("Foo::BAR = 1\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Foo::BAR", "constant") in names

    def test_walks_alias_top_level(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("alias b a\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("b", "alias") in names

    def test_walks_class_with_method_member(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class C\n  def foo\n  end\nend\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C", "class") in names
        assert ("C/foo", "method") in names

    def test_walks_class_with_constant_member(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class C\n  MAX = 10\nend\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/MAX", "constant") in names

    def test_walks_singleton_method(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def self.factory\n    new\n  end\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/self.factory", "singleton_method") in names

    def test_walks_both_instance_and_singleton(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def inst\n  end\n  def self.klass\n  end\nend\n"
        tree = backend.parse(src)
        entries = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/inst", "method") in entries
        assert ("C/self.klass", "singleton_method") in entries

    def test_walks_module_with_member(self, backend: RubyStructuralLanguage) -> None:
        src = "module M\n  def mixed\n  end\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("M", "module") in names
        assert ("M/mixed", "method") in names

    def test_walks_nested_classes(self, backend: RubyStructuralLanguage) -> None:
        src = "class Outer\n  class Inner\n    def f\n    end\n  end\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Outer", "class") in names
        assert ("Outer/Inner", "class") in names
        assert ("Outer/Inner/f", "method") in names

    def test_walks_path_form_class_flat(self, backend: RubyStructuralLanguage) -> None:
        # `class Outer::Inner` at top level walks as flat 'Outer::Inner',
        # not as lexical 'Outer/Inner', because the class body opens at
        # path-form, not nested form.
        src = "class Outer::Inner\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Outer::Inner", "class") in names

    def test_walks_class_inheritance(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class Child < Parent\nend\n")
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("Child", "class") in names

    def test_walks_alias_in_class(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def foo\n  end\n  alias bar foo\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/bar", "alias") in names

    def test_walks_predicate_method(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def empty?\n  end\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/empty?", "method") in names

    def test_walks_bang_method(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def reset!\n  end\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/reset!", "method") in names

    def test_walks_setter_method(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def value=(v)\n  end\nend\n"
        tree = backend.parse(src)
        names = [(n, k) for (n, k, _) in backend.walk_symbols(tree)]
        assert ("C/value=", "method") in names

    def test_walk_extents_point_into_source(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def foo\n  end\nend\n"
        tree = backend.parse(src)
        for name_path, _kind, ref in backend.walk_symbols(tree):
            assert isinstance(ref, _RubySymbolRef)
            sliced = src.encode("utf-8")[ref.extent_offset : ref.extent_offset + ref.extent_length].decode("utf-8")
            if name_path == "C":
                assert "class C" in sliced
            if name_path == "C/foo":
                assert "foo" in sliced

    def test_walk_body_range_is_none_v1(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def foo\n  end\nend\n"
        tree = backend.parse(src)
        for _n, _k, ref in backend.walk_symbols(tree):
            assert ref.body_range is None

    def test_walk_symbols_empty_source(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("")
        assert list(backend.walk_symbols(tree)) == []

    def test_walk_symbols_rejects_non_tree(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            list(backend.walk_symbols("not a tree"))

    def test_walk_ignores_inner_whitespace_for_offsets(self, backend: RubyStructuralLanguage) -> None:
        src = "   \nclass C\nend\n"
        tree = backend.parse(src)
        entries = list(backend.walk_symbols(tree))
        assert any(name == "C" for name, _k, _r in entries)

    def test_walks_require_with_interpolation_skipped(self, backend: RubyStructuralLanguage) -> None:
        # require File.expand_path(...)` -- non-literal argument, should be
        # omitted from the walk.
        src = "require File.expand_path('./x', __dir__)\n"
        tree = backend.parse(src)
        requires = [(n, k) for (n, k, _) in backend.walk_symbols(tree) if k == "require"]
        assert requires == []

    def test_walks_multiple_requires(self, backend: RubyStructuralLanguage) -> None:
        src = "require 'json'\nrequire 'set'\n"
        tree = backend.parse(src)
        names = [n for (n, k, _) in backend.walk_symbols(tree) if k == "require"]
        assert names == ["json", "set"]


# -----------------------------------------------------------------------------
# build_declaration
# -----------------------------------------------------------------------------


class TestBuildDeclaration:
    def test_build_class(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "class Foo\nend"}, [])
        assert d.source == "class Foo\nend\n"

    def test_build_class_with_inheritance(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "class Child < Parent\nend"}, [])
        assert "class Child < Parent" in d.source

    def test_build_class_requires_keyword(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {"statement": "module Foo\nend"}, [])

    def test_build_module(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("module", {"statement": "module M\nend"}, [])
        assert d.source == "module M\nend\n"

    def test_build_module_requires_keyword(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("module", {"statement": "class M\nend"}, [])

    def test_build_require(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("require", {"statement": "require 'json'"}, [])
        assert d.source == "require 'json'\n"

    def test_build_require_relative(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("require", {"statement": "require_relative './util'"}, [])
        assert d.source == "require_relative './util'\n"

    def test_build_require_requires_keyword(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("require", {"statement": "load 'x'"}, [])

    def test_build_constant(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("constant", {"statement": "MAX = 10"}, [])
        assert d.source == "MAX = 10\n"

    def test_build_constant_requires_eq(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("constant", {"statement": "MAX"}, [])

    def test_build_alias(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("alias", {"statement": "alias b a"}, [])
        assert d.source == "alias b a\n"

    def test_build_alias_requires_keyword(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("alias", {"statement": "alias_method :b, :a"}, [])

    def test_build_method_minimal(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("method", {"name": "foo"}, [])
        assert d.source == "def foo\nend\n"

    def test_build_method_with_params(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration(
            "method",
            {"name": "add", "parameters": "a, b", "body": "  a + b"},
            [],
        )
        assert "def add(a, b)" in d.source
        assert "a + b" in d.source

    def test_build_method_with_body(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("method", {"name": "ret", "body": "  42"}, [])
        assert d.source == "def ret\n  42\nend\n"

    def test_build_method_predicate_name(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("method", {"name": "empty?"}, [])
        assert d.source == "def empty?\nend\n"

    def test_build_method_bang_name(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("method", {"name": "reset!"}, [])
        assert d.source == "def reset!\nend\n"

    def test_build_method_setter_name(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("method", {"name": "value=", "parameters": "v"}, [])
        assert d.source == "def value=(v)\nend\n"

    def test_build_method_invalid_name(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {"name": "1bad"}, [])

    def test_build_method_operator_rejected(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {"name": "+"}, [])

    def test_build_singleton_method(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("singleton_method", {"name": "self.build"}, [])
        assert d.source == "def self.build\nend\n"

    def test_build_singleton_method_with_body(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration(
            "singleton_method",
            {"name": "self.factory", "body": "  new"},
            [],
        )
        assert d.source == "def self.factory\n  new\nend\n"

    def test_build_singleton_method_requires_self_prefix(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("singleton_method", {"name": "build"}, [])

    def test_build_method_missing_name_raises(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {}, [])

    def test_build_method_wrong_type_raises(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("method", {"name": 42}, [])

    def test_build_source_file_rejected(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("source_file", {}, [])

    def test_build_unknown_kind_raises(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(KeyError):
            backend.build_declaration("not-a-kind", {}, [])

    def test_build_rejects_foreign_child(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {"statement": "class A\nend"}, ["not a declaration"])


# -----------------------------------------------------------------------------
# insert_child
# -----------------------------------------------------------------------------


class TestInsertChild:
    def test_insert_end_into_empty(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        cls = backend.build_declaration("class", {"statement": "class Foo\nend"}, [])
        new = backend.insert_child(tree, cls, position="end")
        assert "class Foo" in new.source

    def test_insert_end_into_file(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        cls = backend.build_declaration("class", {"statement": "class B\nend"}, [])
        new = backend.insert_child(tree, cls, position="end")
        assert "class A" in new.source
        assert "class B" in new.source

    def test_insert_before_anchor(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\nclass B\nend\n")
        anchor = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "B")
        cls = backend.build_declaration("class", {"statement": "class Mid\nend"}, [])
        new = backend.insert_child(tree, cls, anchor=anchor, position="before")
        assert new.source.index("Mid") < new.source.index("class B")

    def test_insert_after_anchor(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\nclass B\nend\n")
        anchor = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "A")
        cls = backend.build_declaration("class", {"statement": "class Mid\nend"}, [])
        new = backend.insert_child(tree, cls, anchor=anchor, position="after")
        assert new.source.index("class A") < new.source.index("Mid") < new.source.index("class B")

    def test_insert_start_prefixes_source(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        req = backend.build_declaration("require", {"statement": "require 'json'"}, [])
        new = backend.insert_child(tree, req, position="start")
        assert new.source.index("require") < new.source.index("class A")

    def test_insert_method_at_top_level(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        m = backend.build_declaration("method", {"name": "hello"}, [])
        new = backend.insert_child(tree, m, position="end")
        assert "def hello" in new.source

    def test_insert_constant_at_top_level(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        c = backend.build_declaration("constant", {"statement": "MAX = 10"}, [])
        new = backend.insert_child(tree, c, position="end")
        assert "MAX = 10" in new.source

    def test_insert_invalid_position_raises(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        cls = backend.build_declaration("class", {"statement": "class B\nend"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, cls, position="nowhere")

    def test_insert_before_without_anchor_raises(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        cls = backend.build_declaration("class", {"statement": "class B\nend"}, [])
        with pytest.raises(ValueError):
            backend.insert_child(tree, cls, position="before")

    def test_insert_rejects_non_declaration_child(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        with pytest.raises(TypeError):
            backend.insert_child(tree, "not a decl", position="end")

    def test_insert_rejects_non_tree_parent(self, backend: RubyStructuralLanguage) -> None:
        cls = backend.build_declaration("class", {"statement": "class B\nend"}, [])
        with pytest.raises(TypeError):
            backend.insert_child("not a tree", cls, position="end")

    def test_insert_with_symbol_ref_parent_raises(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        ref = next(iter(backend.walk_symbols(tree)))[2]
        cls = backend.build_declaration("class", {"statement": "class B\nend"}, [])
        with pytest.raises(TypeError):
            backend.insert_child(ref, cls, position="end")


# -----------------------------------------------------------------------------
# remove_child
# -----------------------------------------------------------------------------


class TestRemoveChild:
    def test_remove_middle_decl(self, backend: RubyStructuralLanguage) -> None:
        src = "class A\nend\nclass B\nend\nclass C\nend\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "B")
        new = backend.remove_child(tree, ref)
        assert "class B" not in new.source
        assert "class A" in new.source
        assert "class C" in new.source

    def test_remove_require(self, backend: RubyStructuralLanguage) -> None:
        src = "require 'json'\nrequire 'set'\nclass C\nend\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "set")
        new = backend.remove_child(tree, ref)
        assert "'set'" not in new.source
        assert "'json'" in new.source

    def test_remove_method(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def foo\n  end\n  def bar\n  end\nend\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/foo")
        new = backend.remove_child(tree, ref)
        assert "def foo" not in new.source
        assert "def bar" in new.source

    def test_remove_constant_in_class(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  MAX = 10\n  MIN = 1\nend\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/MAX")
        new = backend.remove_child(tree, ref)
        assert "MAX" not in new.source
        assert "MIN = 1" in new.source

    def test_remove_singleton_method(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def self.factory\n  end\n  def inst\n  end\nend\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/self.factory")
        new = backend.remove_child(tree, ref)
        assert "self.factory" not in new.source
        assert "def inst" in new.source

    def test_remove_alias_in_class(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\n  def foo\n  end\n  alias bar foo\nend\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "C/bar")
        new = backend.remove_child(tree, ref)
        assert "alias" not in new.source
        assert "def foo" in new.source

    def test_remove_module(self, backend: RubyStructuralLanguage) -> None:
        src = "module M\nend\nclass C\nend\n"
        tree = backend.parse(src)
        ref = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "M")
        new = backend.remove_child(tree, ref)
        assert "module M" not in new.source
        assert "class C" in new.source

    def test_remove_missing_raises(self, backend: RubyStructuralLanguage) -> None:
        src = "class A\nend\n"
        tree = backend.parse(src)
        fake = _RubySymbolRef(
            kind="class",
            name_path="Nope",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_remove_rejects_non_symbol(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        with pytest.raises(TypeError):
            backend.remove_child(tree, "not a ref")


# -----------------------------------------------------------------------------
# empty_source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        assert backend.serialize(tree) == ""

    def test_empty_source_rejects_unknown_kind(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("not-a-source-kind")


# -----------------------------------------------------------------------------
# serialize
# -----------------------------------------------------------------------------


class TestSerialize:
    def test_serialize_tree(self, backend: RubyStructuralLanguage) -> None:
        src = "class C\nend\n"
        assert backend.serialize(backend.parse(src)) == src

    def test_serialize_declaration(self, backend: RubyStructuralLanguage) -> None:
        d = backend.build_declaration("class", {"statement": "class F\nend"}, [])
        assert backend.serialize(d) == "class F\nend\n"

    def test_serialize_rejects_bad_type(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(TypeError):
            backend.serialize("raw string")


# -----------------------------------------------------------------------------
# Patterns
# -----------------------------------------------------------------------------


class TestPatterns:
    def test_compile_rejects_empty(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_unparseable_pattern_surfaces_on_find(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class C\n  def f\n    x = 1\n  end\nend\n")
        pat = backend.compile_pattern("@@@@")
        with pytest.raises(PatternError):
            list(backend.find_matches(tree, pat))

    def test_find_exact_call_match(self, backend: RubyStructuralLanguage) -> None:
        src = "puts 'hello'\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("puts($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert any("hello" in m.bindings.get("msg", "") for m in matches)

    def test_find_wildcard(self, backend: RubyStructuralLanguage) -> None:
        src = "foo(1, 2)\nfoo(3, 4)\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("foo($_, $_)")
        matches = list(backend.find_matches(tree, pat))
        assert len(matches) >= 2

    def test_find_no_match(self, backend: RubyStructuralLanguage) -> None:
        src = "a.b.c\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("puts($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert matches == []

    def test_find_respects_repeated_capture(self, backend: RubyStructuralLanguage) -> None:
        src = "eq(a, a)\neq(a, b)\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("eq($x, $x)")
        matches = list(backend.find_matches(tree, pat))
        assert len(matches) == 1

    def test_find_method_name_discriminates(self, backend: RubyStructuralLanguage) -> None:
        # Regression guard: pattern 'foo($x, $y)' must NOT match 'bar(1, 2)'.
        src = "foo(1, 2)\nbar(1, 2)\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("foo($x, $y)")
        matches = list(backend.find_matches(tree, pat))
        assert len(matches) == 1
        assert matches[0].bindings == {"x": "1", "y": "2"}

    def test_find_respects_literal(self, backend: RubyStructuralLanguage) -> None:
        # Pattern 'foo(1, $x)' matches foo(1, 2) only, not foo(2, 3).
        src = "foo(1, 2)\nfoo(2, 3)\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("foo(1, $x)")
        matches = list(backend.find_matches(tree, pat))
        assert len(matches) == 1
        assert matches[0].bindings == {"x": "2"}

    def test_find_in_scope(self, backend: RubyStructuralLanguage) -> None:
        src = "class A\n  x(1)\nend\nclass B\n  x(2)\nend\n"
        tree = backend.parse(src)
        scope = next(r for (n, _k, r) in backend.walk_symbols(tree) if n == "B")
        pat = backend.compile_pattern("x($n)")
        matches = list(backend.find_matches(tree, pat, scope=scope))
        assert len(matches) == 1
        assert matches[0].bindings == {"n": "2"}

    def test_render_replacement(self, backend: RubyStructuralLanguage) -> None:
        repl = backend.render_replacement("log($msg)", {"msg": "'hi'"})
        assert repl.source == "log('hi')"

    def test_render_replacement_missing_binding(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("log($msg)", {})

    def test_render_replacement_empty_rejected(self, backend: RubyStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("", {})

    def test_apply_replacement_round_trip(self, backend: RubyStructuralLanguage) -> None:
        src = "log('hi')\n"
        tree = backend.parse(src)
        pat = backend.compile_pattern("log($msg)")
        matches = list(backend.find_matches(tree, pat))
        assert matches
        repl = backend.render_replacement("log2($msg)", {"msg": matches[0].bindings["msg"]})
        new = backend.apply_replacement(tree, matches[0], repl)
        assert "log2" in new.source

    def test_find_matches_rejects_non_tree(self, backend: RubyStructuralLanguage) -> None:
        pat = backend.compile_pattern("foo")
        with pytest.raises(TypeError):
            list(backend.find_matches("not a tree", pat))

    def test_find_matches_rejects_non_pattern(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class C\nend\n")
        with pytest.raises(TypeError):
            list(backend.find_matches(tree, "not a pattern"))


# -----------------------------------------------------------------------------
# Registry exposure
# -----------------------------------------------------------------------------


class TestRegistryExposure:
    def test_ruby_registered(self) -> None:
        registry = default_structural_backend_registry()
        assert "ruby" in registry.registered_languages()

    def test_ruby_extension_routed(self) -> None:
        registry = default_structural_backend_registry()
        be = registry.for_relative_path("lib/foo.rb")
        assert be is not None
        assert be.language_key == "ruby"

    def test_ruby_extension_case_insensitive(self) -> None:
        registry = default_structural_backend_registry()
        be = registry.for_relative_path("Lib/Foo.RB")
        assert be is not None
        assert be.language_key == "ruby"


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


class TestErrorMapping:
    def test_remove_missing_symbol_raises(self, backend: RubyStructuralLanguage) -> None:
        tree = backend.parse("class A\nend\n")
        fake = _RubySymbolRef(
            kind="class",
            name_path="DoesNotExist",
            extent_offset=0,
            extent_length=0,
            body_range=None,
        )
        with pytest.raises(ValueError):
            backend.remove_child(tree, fake)

    def test_apply_replacement_rejects_non_tree(self, backend: RubyStructuralLanguage) -> None:
        from solidlsp.structural.patterns import PatternMatch as _PM

        repl = backend.render_replacement("x", {})
        ref = _RubySymbolRef(kind="match", name_path="", extent_offset=0, extent_length=0, body_range=None)
        pm = _PM(node=ref, bindings={}, symbol_path=None)
        with pytest.raises(TypeError):
            backend.apply_replacement("not a tree", pm, repl)

    def test_apply_replacement_rejects_non_declaration(self, backend: RubyStructuralLanguage) -> None:
        from solidlsp.structural.patterns import PatternMatch as _PM

        tree = backend.parse("class A\nend\n")
        ref = _RubySymbolRef(kind="match", name_path="", extent_offset=0, extent_length=0, body_range=None)
        pm = _PM(node=ref, bindings={}, symbol_path=None)
        with pytest.raises(TypeError):
            backend.apply_replacement(tree, pm, "not a declaration")
