"""Tests for :class:`TomlStructuralLanguage` in
:mod:`solidlsp.structural.backends.toml`.

Mirrors the structure of ``test_yaml.py``: round-trip, kind schema, logical
name resolution, walk-symbols, build_declaration, insert/remove, patterns,
empty source, and registry exposure. Tests for TOML emphasise the
tomlkit-round-trip requirement (sources that don't survive a dump are
rejected at parse time) and the flat table / AoT layout that toml uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from solidlsp.structural.backends.toml import (
    TomlLogicalNameResolver,
    TomlStructuralLanguage,
    toml_kind_schema,
    walk_symbols,
)
from solidlsp.structural.errors import (
    DeclarationError,
    NameResolutionError,
    ParseError,
    PatternError,
)
from solidlsp.structural.registry import default_structural_backend_registry

# =============================================================================
# Identity
# =============================================================================


class TestStructuralLanguageIdentity:
    def test_language_key_is_toml(self) -> None:
        assert TomlStructuralLanguage().language_key == "toml"

    def test_kind_schema_key_matches(self) -> None:
        schema = TomlStructuralLanguage().kind_schema
        assert schema.language_key == "toml"

    def test_name_resolver_defaults_to_cwd(self) -> None:
        backend = TomlStructuralLanguage()
        assert isinstance(backend.name_resolver, TomlLogicalNameResolver)


# =============================================================================
# Parse / serialize round-trip
# =============================================================================


_ROUND_TRIP_SOURCES: list[str] = [
    "",
    "\n",
    "a = 1\n",
    "a = 1\nb = 2\n",
    'name = "alice"\n',
    "count = 42\npi = 3.14\n",
    "flag = true\n",
    "arr = [1, 2, 3]\n",
    "inline = { a = 1, b = 2 }\n",
    "[foo]\nx = 1\n",
    "[foo]\nx = 1\n\n[bar]\ny = 2\n",
    "[a.b.c]\nleaf = 1\n",
    '[[items]]\nname = "one"\n\n[[items]]\nname = "two"\n',
    "# top comment\na = 1\n",
    'text = """\nmulti\nline\n"""\n',
    "hex = 0xff\n",
    "bigint = 1_000_000\n",
]


class TestStructuralParseSerialize:
    @pytest.mark.parametrize("source", _ROUND_TRIP_SOURCES)
    def test_parse_serialize_round_trip(self, source: str) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse(source)
        assert backend.serialize(tree) == source

    def test_serialize_rejects_non_handle(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(TypeError):
            backend.serialize("not a handle")  # type: ignore[arg-type]

    def test_parse_invalid_toml_raises_parse_error(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(ParseError):
            backend.parse("a = = broken\n")

    def test_parse_duplicate_key_raises(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(ParseError):
            backend.parse("a = 1\na = 2\n")


# =============================================================================
# Root / walk
# =============================================================================


class TestStructuralRootKind:
    def test_root_kind_is_document(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        assert backend.root_kind(tree) == "document"

    def test_root_kind_rejects_non_handle(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(TypeError):
            backend.root_kind(object())


class TestStructuralWalkSymbols:
    def test_empty_document_yields_nothing(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("")
        assert list(walk_symbols(tree)) == []

    def test_top_level_pairs_are_yielded(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\n")
        paths = [(p, k) for p, k, _ in walk_symbols(tree)]
        assert paths == [("a", "pair"), ("b", "pair")]

    def test_table_is_walked_as_pair_tree(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("[foo]\nx = 1\ny = 2\n")
        paths = [p for p, _, _ in walk_symbols(tree)]
        assert "foo" in paths
        assert "foo/x" in paths
        assert "foo/y" in paths

    def test_aot_entries_use_bracketed_indices(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse('[[items]]\nname = "one"\n\n[[items]]\nname = "two"\n')
        paths = [(p, k) for p, k, _ in walk_symbols(tree)]
        assert ("items", "pair") in paths
        assert ("items/[0]", "table") in paths
        assert ("items/[1]", "table") in paths
        assert ("items/[0]/name", "pair") in paths

    def test_array_entries_use_bracketed_indices(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("items = [1, 2, 3]\n")
        paths = [(p, k) for p, k, _ in walk_symbols(tree)]
        assert ("items", "pair") in paths
        assert ("items/[0]", "scalar") in paths
        assert ("items/[1]", "scalar") in paths

    def test_nested_tables_are_walked(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("[a.b.c]\nleaf = 1\n")
        paths = [p for p, _, _ in walk_symbols(tree)]
        assert "a" in paths
        assert "a/b" in paths
        assert "a/b/c" in paths
        assert "a/b/c/leaf" in paths

    def test_walk_rejects_non_handle(self) -> None:
        with pytest.raises(TypeError):
            walk_symbols("not a tree")  # type: ignore[arg-type]


# =============================================================================
# Kind schema
# =============================================================================


class TestKindSchema:
    def test_language_key_is_toml(self) -> None:
        assert toml_kind_schema().language_key == "toml"

    def test_document_is_the_only_source_kind(self) -> None:
        assert toml_kind_schema().source_kinds == frozenset({"document"})

    def test_every_expected_kind_is_present(self) -> None:
        assert set(toml_kind_schema().kinds) == {"document", "table", "pair", "array", "aot", "scalar"}

    def test_pair_only_allowed_under_mapping_kinds(self) -> None:
        schema = toml_kind_schema()
        assert schema.get("pair").allowed_parent_kinds == frozenset({"document", "table"})

    def test_aot_only_allowed_under_mapping_kinds(self) -> None:
        schema = toml_kind_schema()
        assert schema.get("aot").allowed_parent_kinds == frozenset({"document", "table"})

    def test_aot_children_are_only_tables(self) -> None:
        schema = toml_kind_schema()
        assert schema.get("aot").allowed_child_kinds == frozenset({"table"})

    def test_scalar_is_a_leaf(self) -> None:
        assert toml_kind_schema().get("scalar").allowed_child_kinds == frozenset()


# =============================================================================
# Logical name resolution
# =============================================================================


class TestLogicalNameResolver:
    def _make(self, tmp_path: Path) -> TomlLogicalNameResolver:
        return TomlLogicalNameResolver(tmp_path)

    def test_parse_slash_form(self, tmp_path: Path) -> None:
        name = self._make(tmp_path).parse("config/db")
        assert name.parts == ("config", "db")
        assert name.raw == "config/db"

    def test_parse_dotted_form(self, tmp_path: Path) -> None:
        name = self._make(tmp_path).parse("config.db")
        assert name.parts == ("config", "db")

    def test_parse_rejects_empty(self, tmp_path: Path) -> None:
        with pytest.raises(NameResolutionError):
            self._make(tmp_path).parse("")

    def test_parse_rejects_invalid_parts(self, tmp_path: Path) -> None:
        with pytest.raises(NameResolutionError):
            self._make(tmp_path).parse("config/db space")

    def test_resolve_existing_toml_file(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text("a = 1\n")
        resolver = self._make(tmp_path)
        resolution = resolver.resolve(resolver.parse("config"))
        assert resolution.relative_path == "config.toml"
        assert resolution.exists is True
        assert resolution.source_kind == "document"

    def test_resolve_nonexistent_synthesizes_creation_path(self, tmp_path: Path) -> None:
        resolver = self._make(tmp_path)
        resolution = resolver.resolve(resolver.parse("new/file"))
        assert resolution.relative_path == str(Path("new") / "file.toml")
        assert resolution.exists is False


# =============================================================================
# Build declaration
# =============================================================================


class TestBuildDeclaration:
    def test_scalar_string(self) -> None:
        decl = TomlStructuralLanguage().build_declaration("scalar", {"value": "hello"}, [])
        assert decl.kind == "scalar"
        assert str(decl.data) == "hello"

    def test_scalar_int(self) -> None:
        decl = TomlStructuralLanguage().build_declaration("scalar", {"value": 42}, [])
        assert int(decl.data) == 42

    def test_scalar_float(self) -> None:
        decl = TomlStructuralLanguage().build_declaration("scalar", {"value": 3.14}, [])
        assert float(decl.data) == 3.14

    def test_scalar_bool(self) -> None:
        decl = TomlStructuralLanguage().build_declaration("scalar", {"value": True}, [])
        assert bool(decl.data) is True

    def test_scalar_null_rejected_for_toml(self) -> None:
        # toml has no native null; the backend rejects None explicitly so
        # callers don't silently get an empty / default-valued declaration
        backend = TomlStructuralLanguage()
        with pytest.raises(DeclarationError):
            backend.build_declaration("scalar", {"value": None}, [])

    def test_scalar_rejects_invalid_type(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(DeclarationError):
            backend.build_declaration("scalar", {"value": object()}, [])

    def test_scalar_rejects_children(self) -> None:
        backend = TomlStructuralLanguage()
        scalar = backend.build_declaration("scalar", {"value": 1}, [])
        with pytest.raises(DeclarationError):
            backend.build_declaration("scalar", {"value": 2}, [scalar])

    def test_pair_wraps_a_scalar(self) -> None:
        backend = TomlStructuralLanguage()
        scalar = backend.build_declaration("scalar", {"value": 1}, [])
        pair = backend.build_declaration("pair", {"key": "a"}, [scalar])
        assert pair.kind == "pair"
        assert pair.key == "a"
        assert int(pair.data) == 1

    def test_pair_requires_exactly_one_child(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(DeclarationError):
            backend.build_declaration("pair", {"key": "a"}, [])

    def test_pair_requires_string_key(self) -> None:
        backend = TomlStructuralLanguage()
        scalar = backend.build_declaration("scalar", {"value": 1}, [])
        with pytest.raises(DeclarationError):
            backend.build_declaration("pair", {"key": 42}, [scalar])  # type: ignore[dict-item]

    def test_table_collects_pairs(self) -> None:
        backend = TomlStructuralLanguage()
        s1 = backend.build_declaration("scalar", {"value": 1}, [])
        s2 = backend.build_declaration("scalar", {"value": 2}, [])
        p1 = backend.build_declaration("pair", {"key": "a"}, [s1])
        p2 = backend.build_declaration("pair", {"key": "b"}, [s2])
        table = backend.build_declaration("table", {}, [p1, p2])
        assert table.kind == "table"
        # table is a tomlkit Table; keys preserved
        assert set(table.data.keys()) == {"a", "b"}

    def test_table_rejects_non_pair_children(self) -> None:
        backend = TomlStructuralLanguage()
        scalar = backend.build_declaration("scalar", {"value": 1}, [])
        with pytest.raises(DeclarationError):
            backend.build_declaration("table", {}, [scalar])

    def test_table_rejects_duplicate_keys(self) -> None:
        backend = TomlStructuralLanguage()
        s1 = backend.build_declaration("scalar", {"value": 1}, [])
        s2 = backend.build_declaration("scalar", {"value": 2}, [])
        p1 = backend.build_declaration("pair", {"key": "a"}, [s1])
        p2 = backend.build_declaration("pair", {"key": "a"}, [s2])
        with pytest.raises(DeclarationError):
            backend.build_declaration("table", {}, [p1, p2])

    def test_array_collects_values(self) -> None:
        backend = TomlStructuralLanguage()
        s1 = backend.build_declaration("scalar", {"value": 1}, [])
        s2 = backend.build_declaration("scalar", {"value": 2}, [])
        arr = backend.build_declaration("array", {}, [s1, s2])
        assert arr.kind == "array"
        assert [int(x) for x in arr.data] == [1, 2]

    def test_array_rejects_pair_child(self) -> None:
        backend = TomlStructuralLanguage()
        scalar = backend.build_declaration("scalar", {"value": 1}, [])
        pair = backend.build_declaration("pair", {"key": "a"}, [scalar])
        with pytest.raises(DeclarationError):
            backend.build_declaration("array", {}, [pair])

    def test_array_rejects_aot_child(self) -> None:
        backend = TomlStructuralLanguage()
        s = backend.build_declaration("scalar", {"value": 1}, [])
        p = backend.build_declaration("pair", {"key": "x"}, [s])
        t = backend.build_declaration("table", {}, [p])
        aot = backend.build_declaration("aot", {}, [t])
        with pytest.raises(DeclarationError):
            backend.build_declaration("array", {}, [aot])

    def test_aot_requires_table_children(self) -> None:
        backend = TomlStructuralLanguage()
        scalar = backend.build_declaration("scalar", {"value": 1}, [])
        with pytest.raises(DeclarationError):
            backend.build_declaration("aot", {}, [scalar])

    def test_aot_collects_tables(self) -> None:
        backend = TomlStructuralLanguage()
        s = backend.build_declaration("scalar", {"value": 1}, [])
        p = backend.build_declaration("pair", {"key": "x"}, [s])
        t1 = backend.build_declaration("table", {}, [p])
        t2 = backend.build_declaration("table", {}, [p])
        aot = backend.build_declaration("aot", {}, [t1, t2])
        assert aot.kind == "aot"
        assert len(aot.data) == 2

    def test_document_cannot_be_built_directly(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(DeclarationError):
            backend.build_declaration("document", {}, [])


# =============================================================================
# Insert / remove
# =============================================================================


class TestInsertChild:
    def _new_pair(self, backend: TomlStructuralLanguage, key: str, value: object) -> object:
        scalar = backend.build_declaration("scalar", {"value": value}, [])
        return backend.build_declaration("pair", {"key": key}, [scalar])

    def test_append_to_mapping_at_end(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        pair = self._new_pair(backend, "b", 2)
        out = backend.insert_child(tree, pair, position="end")
        assert backend.serialize(out) == "a = 1\nb = 2\n"

    def test_prepend_to_mapping_at_start(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        pair = self._new_pair(backend, "zero", 0)
        out = backend.insert_child(tree, pair, position="start")
        assert backend.serialize(out) == "zero = 0\na = 1\n"

    def test_insert_before_anchor(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nc = 3\n")
        anchor = next(node for p, _, node in walk_symbols(tree) if p == "c")
        pair = self._new_pair(backend, "b", 2)
        out = backend.insert_child(tree, pair, anchor=anchor, position="before")
        assert backend.serialize(out) == "a = 1\nb = 2\nc = 3\n"

    def test_insert_after_anchor(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nc = 3\n")
        anchor = next(node for p, _, node in walk_symbols(tree) if p == "a")
        pair = self._new_pair(backend, "b", 2)
        out = backend.insert_child(tree, pair, anchor=anchor, position="after")
        assert backend.serialize(out) == "a = 1\nb = 2\nc = 3\n"

    def test_insert_before_without_anchor_raises(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        pair = self._new_pair(backend, "b", 2)
        with pytest.raises(ValueError):
            backend.insert_child(tree, pair, position="before")

    def test_insert_end_places_pair_before_tables(self) -> None:
        # TOML syntax requires top-level scalar/array pairs to precede any
        # [table] / [[aot]] header. A naive body.append would drop the new
        # pair into the last table's scope; the backend places it right after
        # the last non-container keyed entry so the top-level intent is kept.
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n\n[foo]\nx = 1\n")
        pair = self._new_pair(backend, "b", 2)
        out = backend.insert_child(tree, pair, position="end")
        rendered = backend.serialize(out)
        # b must come before the [foo] header, not inside foo
        assert rendered.index("b = 2") < rendered.index("[foo]")

    def test_insert_wrong_kind_rejected(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        scalar = backend.build_declaration("scalar", {"value": 2}, [])
        with pytest.raises(TypeError):
            backend.insert_child(tree, scalar)

    def test_duplicate_key_insert_rejected(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        pair = self._new_pair(backend, "a", 99)
        with pytest.raises(DeclarationError):
            backend.insert_child(tree, pair, position="end")

    def test_insert_table_without_pair_wrapping_rejected(self) -> None:
        # the backend requires tables/aots to be wrapped in a pair (with a
        # key) before insertion because a bare table declaration has no name
        # -- treated as a wrong-kind insertion at the mapping level
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        s = backend.build_declaration("scalar", {"value": 1}, [])
        p = backend.build_declaration("pair", {"key": "x"}, [s])
        table = backend.build_declaration("table", {}, [p])
        with pytest.raises(TypeError):
            backend.insert_child(tree, table)


class TestRemoveChild:
    def test_remove_key_from_document(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\n")
        anchor = next(node for p, _, node in walk_symbols(tree) if p == "b")
        out = backend.remove_child(tree, anchor)
        assert backend.serialize(out) == "a = 1\n"

    def test_remove_unknown_raises(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(ValueError):
            backend.remove_child(tree, (object(), "not-a-key"))


class TestContainerInsertMember:
    """L3 path-based insertion into mappings and sequences.

    Exercises :meth:`TomlStructuralLanguage.container_insert_member`. Paths
    are slash-separated; mapping segments are bare keys, sequence segments
    are ``[N]``. ``position="start"/"end"`` targets the container at
    ``anchor_or_container_path``; ``"before"/"after"`` targets the named
    sibling member instead.
    """

    def test_insert_end_root_mapping(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\n")
        out = backend.container_insert_member(tree, "", "c = 3", position="end")
        assert backend.serialize(out) == "a = 1\nb = 2\nc = 3\n"

    def test_insert_start_root_mapping(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\n")
        out = backend.container_insert_member(tree, "", "z = 0", position="start")
        assert backend.serialize(out) == "z = 0\na = 1\nb = 2\n"

    def test_insert_before_anchor(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\n")
        out = backend.container_insert_member(tree, "b", "mid = 99", position="before")
        assert backend.serialize(out) == "a = 1\nmid = 99\nb = 2\n"

    def test_insert_after_anchor(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\n")
        out = backend.container_insert_member(tree, "a", "mid = 99", position="after")
        assert backend.serialize(out) == "a = 1\nmid = 99\nb = 2\n"

    def test_insert_into_nested_table(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse('[pkg]\nname = "hi"\nversion = "1"\n')
        out = backend.container_insert_member(tree, "pkg", 'author = "me"', position="end")
        assert backend.serialize(out) == '[pkg]\nname = "hi"\nversion = "1"\nauthor = "me"\n'

    def test_insert_after_anchor_in_deep_table(self) -> None:
        # canonical-ish repro: deep [tool.x] with a multi-line body, insert
        # a new pair between two existing ones; surrounding pairs and the
        # table header must be untouched
        backend = TomlStructuralLanguage()
        tree = backend.parse("[tool.x]\nfirst = 1\nsecond = 2\nthird = 3\n")
        out = backend.container_insert_member(
            tree,
            "tool/x/second",
            "between = 99",
            position="after",
        )
        assert backend.serialize(out) == "[tool.x]\nfirst = 1\nsecond = 2\nbetween = 99\nthird = 3\n"

    def test_insert_into_inline_array_end(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("xs = [1, 2, 3]\n")
        out = backend.container_insert_member(tree, "xs", "4", position="end")
        assert backend.serialize(out) == "xs = [1, 2, 3, 4]\n"

    def test_insert_into_inline_array_before_index(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("xs = [1, 2, 3]\n")
        out = backend.container_insert_member(tree, "xs/[1]", "99", position="before")
        assert backend.serialize(out) == "xs = [1, 99, 2, 3]\n"

    def test_insert_rejects_non_tree(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(TypeError):
            backend.container_insert_member("not a tree", "", "a = 1")  # type: ignore[arg-type]

    def test_insert_rejects_invalid_position(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(ValueError):
            backend.container_insert_member(tree, "", "b = 2", position="middle")

    def test_insert_rejects_duplicate_key(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(DeclarationError):
            backend.container_insert_member(tree, "", "a = 2", position="end")

    def test_insert_rejects_missing_anchor(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(ValueError):
            backend.container_insert_member(tree, "missing", "b = 2", position="before")

    def test_insert_rejects_malformed_member_source(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(ParseError):
            backend.container_insert_member(tree, "", "not even a pair!", position="end")


class TestContainerRemoveMember:
    """L3 path-based removal from mappings and sequences."""

    def test_remove_from_root_mapping(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\nc = 3\n")
        out = backend.container_remove_member(tree, "b")
        assert backend.serialize(out) == "a = 1\nc = 3\n"

    def test_remove_from_nested_table(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse('[pkg]\nname = "hi"\nversion = "1"\n')
        out = backend.container_remove_member(tree, "pkg/version")
        assert backend.serialize(out) == '[pkg]\nname = "hi"\n'

    def test_remove_from_inline_array(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("xs = [1, 2, 3]\n")
        out = backend.container_remove_member(tree, "xs/[1]")
        assert backend.serialize(out) == "xs = [1, 3]\n"

    def test_remove_rejects_non_tree(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(TypeError):
            backend.container_remove_member("not a tree", "a")  # type: ignore[arg-type]

    def test_remove_rejects_missing_key(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(ValueError):
            backend.container_remove_member(tree, "nonexistent")

    def test_remove_rejects_out_of_range_index(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("xs = [1, 2]\n")
        with pytest.raises(ValueError):
            backend.container_remove_member(tree, "xs/[5]")


class TestContainerReplaceMember:
    """L3 path-based value replacement for mapping entries and sequence items."""

    def test_replace_root_mapping_value(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\nb = 2\n")
        out = backend.container_replace_member(tree, "a", "99")
        assert backend.serialize(out) == "a = 99\nb = 2\n"

    def test_replace_nested_mapping_value(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse('[pkg]\nname = "hi"\nversion = "1"\n')
        out = backend.container_replace_member(tree, "pkg/name", '"bye"')
        assert backend.serialize(out) == '[pkg]\nname = "bye"\nversion = "1"\n'

    def test_replace_inline_array_item(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("xs = [1, 2, 3]\n")
        out = backend.container_replace_member(tree, "xs/[1]", "99")
        assert backend.serialize(out) == "xs = [1, 99, 3]\n"

    def test_replace_rejects_non_tree(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(TypeError):
            backend.container_replace_member("not a tree", "a", "1")  # type: ignore[arg-type]

    def test_replace_rejects_missing_key(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(ValueError):
            backend.container_replace_member(tree, "missing", "99")

    def test_replace_rejects_malformed_value(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        with pytest.raises(ParseError):
            backend.container_replace_member(tree, "a", "not-a-value!!")


# =============================================================================
# Empty source
# =============================================================================


class TestEmptySource:
    def test_empty_document(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.empty_source("document")
        assert backend.serialize(tree) == ""

    def test_rejects_non_document_kind(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(DeclarationError):
            backend.empty_source("table")


# =============================================================================
# Patterns
# =============================================================================


class TestCompilePattern:
    def test_compile_valid_toml(self) -> None:
        backend = TomlStructuralLanguage()
        pattern = backend.compile_pattern("a = 1\n")
        assert pattern is not None

    def test_compile_empty_raises(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_compile_comment_only_raises(self) -> None:
        # a pattern with no key entries has nothing to match against
        backend = TomlStructuralLanguage()
        with pytest.raises(PatternError):
            backend.compile_pattern("# just a comment\n")

    def test_compile_invalid_toml_raises(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(PatternError):
            backend.compile_pattern("a = = broken\n")


class TestFindMatches:
    def test_exact_match_against_root(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        pattern = backend.compile_pattern("a = 1\n")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1

    def test_wildcard_matches_any_value(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse('name = "alice"\nage = 42\n')
        pattern = backend.compile_pattern('name = "$_"\nage = "$_"\n')
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1

    def test_capture_binds_value(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse('name = "alice"\nage = 42\n')
        pattern = backend.compile_pattern('name = "$who"\nage = "$n"\n')
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1
        assert matches[0].bindings["who"] == "alice"
        assert int(matches[0].bindings["n"]) == 42

    def test_nested_match_finds_each_aot_entry(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse(
            '[[items]]\nname = "alice"\nage = 30\n\n[[items]]\nname = "bob"\nage = 40\n',
        )
        pattern = backend.compile_pattern('name = "$who"\nage = "$n"\n')
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 2
        assert {str(m.bindings["who"]) for m in matches} == {"alice", "bob"}

    def test_key_set_mismatch_does_not_match(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse('name = "alice"\n')
        pattern = backend.compile_pattern('name = "$who"\nage = "$n"\n')
        matches = list(backend.find_matches(tree, pattern))
        assert matches == []

    def test_array_length_mismatch_does_not_match(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("arr = [1, 2, 3]\n")
        pattern = backend.compile_pattern('arr = ["$a", "$b"]\n')
        matches = list(backend.find_matches(tree, pattern))
        assert matches == []


class TestRenderReplacement:
    def test_render_substitutes_capture(self) -> None:
        backend = TomlStructuralLanguage()
        repl = backend.render_replacement('name = "$who"\n', {"who": "alice"})
        assert "alice" in repl.data.as_string()

    def test_missing_capture_raises(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(PatternError):
            backend.render_replacement('name = "$who"\n', {})

    def test_wildcard_in_replacement_raises(self) -> None:
        backend = TomlStructuralLanguage()
        with pytest.raises(PatternError):
            backend.render_replacement('name = "$_"\n', {})


class TestApplyReplacement:
    def test_replace_at_root(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse("a = 1\n")
        pattern = backend.compile_pattern('a = "$n"\n')
        match = next(iter(backend.find_matches(tree, pattern)))
        repl = backend.render_replacement("a = 99\n", {})
        out = backend.apply_replacement(tree, match, repl)
        assert backend.serialize(out) == "a = 99\n"

    def test_replace_preserves_siblings(self) -> None:
        backend = TomlStructuralLanguage()
        tree = backend.parse(
            '[[items]]\nname = "alice"\nage = 30\n\n[[items]]\nname = "bob"\nage = 40\n',
        )
        pattern = backend.compile_pattern('name = "alice"\nage = "$n"\n')
        match = next(iter(backend.find_matches(tree, pattern)))
        repl = backend.render_replacement('name = "ALICE"\nage = 99\n', {})
        out = backend.apply_replacement(tree, match, repl)
        rendered = backend.serialize(out)
        assert "ALICE" in rendered
        assert "bob" in rendered


# =============================================================================
# Registry exposure
# =============================================================================


class TestRegistryExposure:
    def test_toml_key_resolves(self) -> None:
        registry = default_structural_backend_registry()
        backend = registry.for_language("toml")
        assert backend is not None
        assert backend.language_key == "toml"

    def test_toml_extension_resolves(self) -> None:
        registry = default_structural_backend_registry()
        backend = registry.for_relative_path("config/pyproject.toml")
        assert backend is not None
        assert backend.language_key == "toml"

    def test_registered_language_keys_includes_toml(self) -> None:
        registry = default_structural_backend_registry()
        assert "toml" in registry.registered_languages()
