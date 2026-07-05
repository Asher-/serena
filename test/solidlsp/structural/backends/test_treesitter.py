"""Unit tests for the tree-sitter universal structural fallback backend (spec-v2 §5.8).

The backend is READ + byte-span-write only. It parses any language the
``tree-sitter-language-pack`` ships, round-trips bytes verbatim, renders a
walked node's exact source slice, and reports the node's 0-based line span.
Structural editing is unsupported -- the write-side ABC methods raise
:class:`NotImplementedError` (byte-span writes go through
``cursor_replace_range``, which never touches the backend). Malformed source
never raises: the parse tree carries an error flag and rendering still returns
the raw slice (the ERROR-fallthrough guarantee).
"""

from __future__ import annotations

import pytest

from solidlsp.structural.backends.python import PythonStructuralLanguage
from solidlsp.structural.backends.treesitter import (
    TreeSitterLanguage,
    tree_sitter_fallback_resolver,
)
from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.registry import default_structural_backend_registry

# fixtures in three languages the pack ships but the explicit 13 do NOT own
_CSS = "a {\n  color: red;\n}\n"
_SH = "#!/bin/sh\necho hi\n"
_SQL = "select 1;\n"


class TestRoundTrip:
    """``serialize(parse(s)) == s`` byte-for-byte -- the structural admissibility contract."""

    @pytest.mark.parametrize(("lang", "source"), [("css", _CSS), ("bash", _SH), ("sql", _SQL)])
    def test_serialize_parse_is_byte_identical(self, lang: str, source: str) -> None:
        backend = TreeSitterLanguage(lang)
        assert backend.serialize(backend.parse(source)) == source

    def test_empty_source_parses_and_round_trips(self) -> None:
        backend = TreeSitterLanguage("css")
        tree = backend.parse("")
        assert backend.serialize(tree) == ""

    def test_backend_is_a_structural_language(self) -> None:
        assert isinstance(TreeSitterLanguage("css"), StructuralLanguage)


class TestWalkAndRender:
    """A walked node renders exactly its source span and reports a 0-based line range."""

    def test_walk_symbols_yields_addressable_nodes(self) -> None:
        backend = TreeSitterLanguage("css")
        walked = list(backend.walk_symbols(backend.parse(_CSS)))
        assert walked  # at least one (name_path, kind, node)
        name_path, kind, _node = walked[0]
        assert isinstance(name_path, str) and name_path
        assert isinstance(kind, str) and kind

    def test_render_node_source_is_the_exact_byte_slice(self) -> None:
        # structural-symmetry: every walked node renders exactly source[start:end]
        backend = TreeSitterLanguage("css")
        source_bytes = _CSS.encode("utf-8")
        for _name_path, _kind, node in backend.walk_symbols(backend.parse(_CSS)):
            rendered = backend.render_node_source(node)
            assert rendered == source_bytes[node.start_byte : node.end_byte].decode("utf-8")
            assert rendered in _CSS

    def test_node_line_range_is_zero_based_rows(self) -> None:
        backend = TreeSitterLanguage("css")
        walked = list(backend.walk_symbols(backend.parse(_CSS)))
        top = walked[0][2]
        rng = backend.node_line_range(top)
        assert rng == (top.start_row, top.end_row)
        # the first top-level node begins on the file's first line -> 0-based row 0
        assert rng[0] == 0
        assert isinstance(rng[0], int) and isinstance(rng[1], int)


class TestErrorFallthrough:
    """Malformed source is a rendered state, never a raise (spec-v2 §5.8 ERROR-fallthrough)."""

    def test_malformed_source_does_not_raise_and_flags_error(self) -> None:
        backend = TreeSitterLanguage("css")
        tree = backend.parse("a {")  # unterminated rule
        assert tree.has_error  # the tree carries the error rather than raising
        assert backend.serialize(tree) == "a {"  # round-trip still holds byte-for-byte

    def test_render_still_works_over_malformed_source(self) -> None:
        backend = TreeSitterLanguage("css")
        tree = backend.parse("a {")
        source_bytes = b"a {"
        for _name_path, _kind, node in backend.walk_symbols(tree):
            assert backend.render_node_source(node) == source_bytes[node.start_byte : node.end_byte].decode("utf-8")


class TestWriteMethodsRejected:
    """Structural editing is DEFERRED: every write-side ABC method raises NotImplementedError."""

    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("build_declaration", ("rule", {}, [])),
            ("insert_child", (object(), object())),
            ("remove_child", (object(), object())),
            ("compile_pattern", ("x",)),
            ("find_matches", (object(), object())),
            ("render_replacement", ("x", {})),
            ("apply_replacement", (object(), object(), object())),
            ("empty_source", ("source",)),
        ],
    )
    def test_write_side_method_raises_not_implemented(self, method: str, args: tuple) -> None:
        backend = TreeSitterLanguage("css")
        with pytest.raises(NotImplementedError, match="read"):
            getattr(backend, method)(*args)


class TestFallbackResolver:
    """The module resolver maps only NEW (unowned) extensions/basenames to a memoized backend."""

    def test_known_extension_returns_memoized_backend(self) -> None:
        first = tree_sitter_fallback_resolver("styles.css")
        second = tree_sitter_fallback_resolver("other.css")
        assert isinstance(first, TreeSitterLanguage)
        assert first is second  # one backend instance per language

    def test_basename_mapped_language(self) -> None:
        assert isinstance(tree_sitter_fallback_resolver("Dockerfile"), TreeSitterLanguage)

    def test_unknown_extension_is_none(self) -> None:
        assert tree_sitter_fallback_resolver("mystery.unknownext") is None

    def test_extensionless_unmapped_is_none(self) -> None:
        assert tree_sitter_fallback_resolver("LICENSE") is None


class TestRegistryRouting:
    """``structural_backend_for`` adds tree-sitter BELOW the explicit 13, never displacing them."""

    def test_new_extension_routes_to_treesitter(self) -> None:
        registry = default_structural_backend_registry()
        assert isinstance(registry.structural_backend_for("styles.css"), TreeSitterLanguage)
        assert isinstance(registry.structural_backend_for("Dockerfile"), TreeSitterLanguage)

    def test_explicit_backend_still_wins(self) -> None:
        registry = default_structural_backend_registry()
        # .py is owned by the explicit Python backend -- tree-sitter must NOT shadow it
        assert isinstance(registry.structural_backend_for("m.py"), PythonStructuralLanguage)
        # .json is a structural-only explicit backend -- still explicit, not tree-sitter
        assert not isinstance(registry.structural_backend_for("data.json"), TreeSitterLanguage)

    def test_default_backend_for_unknown_extension_defers_to_explicit(self) -> None:
        registry = default_structural_backend_registry()
        # an explicitly-owned extension yields None from the fallback path (explicit wins)
        assert registry.default_backend_for_unknown_extension("m.py") is None

    def test_truly_unknown_extension_is_none(self) -> None:
        registry = default_structural_backend_registry()
        # no explicit backend and no tree-sitter language -> None -> plaintext floor
        assert registry.structural_backend_for("notes.unknownext") is None
