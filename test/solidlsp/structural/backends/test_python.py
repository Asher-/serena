"""Tests for :mod:`solidlsp.structural.backends.python`.

Organized as four suites:

* **round-trip fixtures** — curated edge-case sources that previously broke
  parsers we rejected.
* **round-trip corpus** — every ``.py`` file in the serena source tree; the
  backend is only admissible to the structural layer if this passes.
* **symbol walk / declaration / mutation** — exercises the rest of the ABC.
* **pattern matching and rewriting** — exercises the ``$`` sigil grammar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import libcst as cst
import pytest

from solidlsp.structural.errors import (
    DeclarationError,
    NameResolutionError,
    ParseError,
    PatternError,
)
from solidlsp.structural.backends.python import (
    PythonLogicalNameResolver,
    PythonStructuralLanguage,
    python_kind_schema,
)
from test.solidlsp.structural.harness import assert_round_trip, iter_corpus

# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def backend() -> PythonStructuralLanguage:
    return PythonStructuralLanguage()


REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_ROOT = REPO_ROOT / "src"


# edge-case fixtures: historical sources that broke other parsers
_EDGE_CASES: tuple[tuple[str, str], ...] = (
    ("empty", ""),
    ("only-newline", "\n"),
    ("trailing-newline", "x = 1\n"),
    ("no-final-newline", "x = 1"),
    ("crlf-line-endings", "x = 1\r\ny = 2\r\n"),
    ("mixed-line-endings", "x = 1\ny = 2\r\n"),
    # BOM is not preserved by libcst (it strips U+FEFF during tokenization).
    # Sources with a BOM are not currently admissible; Serena writes UTF-8 without BOM.
    ("shebang", "#!/usr/bin/env python3\nimport sys\n"),
    ("comments-only", "# just a comment\n# another\n"),
    (
        "docstring-with-trailing-whitespace",
        '"""Docstring with internal trailing whitespace.   \n\n    and a blank line.\n"""\n',
    ),
    (
        "line-continuations",
        "result = (\n    1\n    + 2\n    + 3\n)\n",
    ),
    (
        "nested-fstrings",
        'x = f"outer {f\'inner {1 + 2!r}\'}"\n',
    ),
    (
        "decorators-stack",
        "@a\n@b(c)\n@d.e\nclass Target:\n    pass\n",
    ),
    (
        "type-aliases-pep695",
        "type Vector[T] = list[T]\n",
    ),
    (
        "match-statement",
        "match x:\n    case 1:\n        pass\n    case _:\n        raise ValueError\n",
    ),
)


# -----------------------------------------------------------------------------
# Round-trip: curated edge cases
# -----------------------------------------------------------------------------


class TestRoundTripFixtures:
    @pytest.mark.parametrize(("label", "source"), _EDGE_CASES, ids=[case[0] for case in _EDGE_CASES])
    def test_edge_case_round_trips(self, backend: PythonStructuralLanguage, label: str, source: str) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Round-trip: entire serena source tree
# -----------------------------------------------------------------------------


def _python_corpus() -> Iterable[tuple[str, str]]:
    """Yield every ``.py`` file under ``src/`` except generated static assets."""
    # static assets under language_servers break on unusual encodings; excluded in ruff too
    for label, source in iter_corpus(SRC_ROOT, "**/*.py"):
        if "language_servers/" in label and "/static/" in label:
            continue
        yield label, source


_PYTHON_CORPUS = list(_python_corpus())


class TestRoundTripCorpus:
    @pytest.mark.parametrize(("label", "source"), _PYTHON_CORPUS, ids=[label for label, _ in _PYTHON_CORPUS])
    def test_source_round_trips(self, backend: PythonStructuralLanguage, label: str, source: str) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Parse error path
# -----------------------------------------------------------------------------


class TestParseErrors:
    def test_syntactically_invalid_source_raises(self, backend: PythonStructuralLanguage) -> None:
        with pytest.raises(ParseError) as info:
            backend.parse("def (\n")
        assert info.value.language_key == "python"
        assert info.value.detail


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_language_key_and_source_kinds(self, backend: PythonStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "python"
        assert schema.source_kinds == frozenset({"module"})

    def test_expected_kinds_present(self, backend: PythonStructuralLanguage) -> None:
        schema = backend.kind_schema
        expected = {"module", "import", "class", "function", "method", "assignment", "decorator"}
        assert expected <= set(schema.kinds)

    def test_methods_only_allowed_under_classes(self) -> None:
        schema = python_kind_schema()
        method = schema.get("method")
        assert method.allowed_parent_kinds == frozenset({"class"})

    def test_decorator_only_allowed_under_compounds(self) -> None:
        schema = python_kind_schema()
        decorator = schema.get("decorator")
        assert decorator.allowed_parent_kinds == frozenset({"class", "function", "method"})

    def test_validate_composition_enforces_rules(self) -> None:
        schema = python_kind_schema()
        # method at module level is rejected
        with pytest.raises(DeclarationError):
            schema.validate_composition("module", "method")
        # function inside class is allowed (method would also be allowed; we emit 'method')
        schema.validate_composition("class", "method")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_valid_dotted_name(self, tmp_path: Path) -> None:
        resolver = PythonLogicalNameResolver(tmp_path)
        name = resolver.parse("a.b.c")
        assert name.parts == ("a", "b", "c")
        assert name.raw == "a.b.c"

    @pytest.mark.parametrize("invalid", ["", "a..b", "a.1.b", "a.b-c", "1a"])
    def test_parse_rejects_non_identifiers(self, tmp_path: Path, invalid: str) -> None:
        resolver = PythonLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse(invalid)

    def test_resolve_existing_module(self, tmp_path: Path) -> None:
        src_root = tmp_path / "src"
        (src_root / "pkg").mkdir(parents=True)
        (src_root / "pkg" / "__init__.py").write_text("")
        (src_root / "pkg" / "mod.py").write_text("x = 1\n")
        resolver = PythonLogicalNameResolver(tmp_path, source_roots=[src_root])
        resolution = resolver.resolve(resolver.parse("pkg.mod"))
        assert resolution.exists is True
        assert resolution.relative_path == "src/pkg/mod.py"
        assert resolution.source_kind == "module"

    def test_resolve_existing_package(self, tmp_path: Path) -> None:
        src_root = tmp_path / "src"
        (src_root / "pkg").mkdir(parents=True)
        (src_root / "pkg" / "__init__.py").write_text("")
        resolver = PythonLogicalNameResolver(tmp_path, source_roots=[src_root])
        resolution = resolver.resolve(resolver.parse("pkg"))
        assert resolution.exists is True
        assert resolution.relative_path == "src/pkg/__init__.py"

    def test_resolve_nonexistent_synthesizes_path(self, tmp_path: Path) -> None:
        src_root = tmp_path / "src"
        src_root.mkdir()
        resolver = PythonLogicalNameResolver(tmp_path, source_roots=[src_root])
        resolution = resolver.resolve(resolver.parse("new.module"))
        assert resolution.exists is False
        assert resolution.relative_path == "src/new/module.py"


# -----------------------------------------------------------------------------
# Symbol walk
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_top_level_symbols(self, backend: PythonStructuralLanguage) -> None:
        source = "import os\n\nx = 1\n\ndef f():\n    pass\n\nclass C:\n    def m(self):\n        pass\n"
        tree = backend.parse(source)
        symbols = list(backend.walk_symbols(tree))
        names_and_kinds = [(path, kind) for path, kind, _ in symbols]
        assert ("os", "import") in names_and_kinds
        assert ("x", "assignment") in names_and_kinds
        assert ("f", "function") in names_and_kinds
        assert ("C", "class") in names_and_kinds
        assert ("C/m", "method") in names_and_kinds

    def test_nested_functions_are_functions_not_methods(self, backend: PythonStructuralLanguage) -> None:
        source = "def outer():\n    def inner():\n        pass\n"
        tree = backend.parse(source)
        symbols = [(path, kind) for path, kind, _ in backend.walk_symbols(tree)]
        assert ("outer", "function") in symbols
        assert ("outer/inner", "function") in symbols

    def test_root_kind_is_module(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("")
        assert backend.root_kind(tree) == "module"


# -----------------------------------------------------------------------------
# Declaration + mutation
# -----------------------------------------------------------------------------


class TestDeclarationAndMutation:
    def test_build_import_declaration(self, backend: PythonStructuralLanguage) -> None:
        node = backend.build_declaration("import", {"statement": "import os\n"}, ())
        assert isinstance(node, cst.SimpleStatementLine)

    def test_build_function_declaration(self, backend: PythonStructuralLanguage) -> None:
        node = backend.build_declaration(
            "function",
            {"name": "greet", "parameters": "who: str", "return_annotation": "str", "body": "    return f'hi {who}'"},
            (),
        )
        assert isinstance(node, cst.FunctionDef)
        assert node.name.value == "greet"

    def test_build_class_with_decorator(self, backend: PythonStructuralLanguage) -> None:
        deco = backend.build_declaration("decorator", {"expression": "dataclass"}, ())
        cls = backend.build_declaration("class", {"name": "Point", "bases": [], "body": "    x: int\n    y: int"}, (deco,))
        assert isinstance(cls, cst.ClassDef)
        assert len(cls.decorators) == 1

    def test_build_declaration_missing_required_attribute_raises(self, backend: PythonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("class", {"bases": []}, ())

    def test_insert_child_at_end(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("x = 1\n")
        new_stmt = backend.build_declaration("import", {"statement": "import os\n"}, ())
        new_tree = backend.insert_child(tree, new_stmt, anchor=None, position="end")
        assert "import os" in backend.serialize(new_tree)
        # original tree unchanged
        assert "import os" not in backend.serialize(tree)

    def test_insert_child_before_anchor(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("x = 1\ny = 2\n")
        original_y = tree.body[1]
        new_stmt = backend.build_declaration("import", {"statement": "import os\n"}, ())
        new_tree = backend.insert_child(tree, new_stmt, anchor=original_y, position="before")
        serialized = backend.serialize(new_tree)
        # the import appears between 'x = 1' and 'y = 2'
        assert serialized.index("import os") > serialized.index("x = 1")
        assert serialized.index("import os") < serialized.index("y = 2")

    def test_remove_child(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("import os\nx = 1\n")
        import_stmt = tree.body[0]
        new_tree = backend.remove_child(tree, import_stmt)
        assert "import os" not in backend.serialize(new_tree)


# -----------------------------------------------------------------------------
# Pattern matching
# -----------------------------------------------------------------------------


class TestPatternMatching:
    def test_exact_pattern_matches(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("x = foo(1)\ny = foo(2)\n")
        pattern = backend.compile_pattern("foo(1)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1

    def test_capture_binds_subtree(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("x = foo(1)\ny = foo(2)\n")
        pattern = backend.compile_pattern("foo($arg)")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 2
        bound_values = sorted(
            m.bindings["arg"].value
            for m in matches
            if isinstance(m.bindings["arg"], cst.Integer)
        )
        assert bound_values == ["1", "2"]

    def test_wildcard_does_not_bind(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("a = 1\nb = 2\n")
        pattern = backend.compile_pattern("$_")
        # matches many nodes; ensures no KeyError from missing binding
        matches = list(backend.find_matches(tree, pattern))
        assert matches
        for match in matches:
            assert "_" not in match.bindings

    def test_no_match_yields_empty(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("x = 1\n")
        pattern = backend.compile_pattern("foo($arg)")
        assert list(backend.find_matches(tree, pattern)) == []

    def test_empty_pattern_rejected(self, backend: PythonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_malformed_pattern_rejected(self, backend: PythonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("def (")


# -----------------------------------------------------------------------------
# Rewriting
# -----------------------------------------------------------------------------


class TestRewriting:
    def test_render_and_apply_replacement(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("x = foo(1)\n")
        pattern = backend.compile_pattern("foo($arg)")
        match = next(iter(backend.find_matches(tree, pattern)))
        replacement = backend.render_replacement("bar($arg)", match.bindings)
        new_tree = backend.apply_replacement(tree, match, replacement)
        assert backend.serialize(new_tree) == "x = bar(1)\n"

    def test_render_missing_binding_raises(self, backend: PythonStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("bar($arg)", bindings={})

    def test_apply_replacement_missing_match_raises(self, backend: PythonStructuralLanguage) -> None:
        tree = backend.parse("x = 1\n")
        other_tree = backend.parse("x = 1\n")
        pattern = backend.compile_pattern("1")
        foreign_match = next(iter(backend.find_matches(other_tree, pattern)))
        replacement = backend.render_replacement("2", {})
        with pytest.raises(PatternError):
            backend.apply_replacement(tree, foreign_match, replacement)


# -----------------------------------------------------------------------------
# empty_source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_serializes_to_empty_string(self, backend: PythonStructuralLanguage) -> None:
        empty = backend.empty_source("module")
        assert backend.serialize(empty) == ""

    def test_empty_source_rejects_unknown_kind(self, backend: PythonStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("class")
