"""Tests for :mod:`solidlsp.structural.backends.markdown`.

Organized into suites mirroring the Python and C++ backends' layout:

* **round-trip fixtures** — curated markdown edge cases.
* **parse errors** — markdown-it-py tolerates almost everything; verify it.
* **kind schema** — shape of the published kind vocabulary.
* **name resolver** — logical dotted/slashed path → ``.md`` file.
* **symbol walk** — heading hierarchy yields the right structural paths.
* **declaration + mutation** — build_declaration + insert_child + remove_child.
* **pattern matching and rewriting** — ``$``-sigil grammar over block nodes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from solidlsp.structural.backends.markdown import (
    MarkdownLogicalNameResolver,
    MarkdownStructuralLanguage,
    _MdSymbolRef,
    markdown_kind_schema,
)
from solidlsp.structural.errors import (
    DeclarationError,
    NameResolutionError,
    PatternError,
)
from test.solidlsp.structural.harness import assert_round_trip


# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def backend() -> MarkdownStructuralLanguage:
    return MarkdownStructuralLanguage()


_EDGE_CASES: tuple[tuple[str, str], ...] = (
    ("empty", ""),
    ("only-newline", "\n"),
    ("trailing-newline", "# Hello\n"),
    ("no-final-newline", "# Hello"),
    ("crlf-line-endings", "# A\r\n\r\nbody\r\n"),
    ("mixed-line-endings", "# A\n\nbody\r\n"),
    ("atx-all-levels", "# h1\n\n## h2\n\n### h3\n\n#### h4\n\n##### h5\n\n###### h6\n"),
    ("setext-heading", "Title\n=====\n\nbody\n"),
    ("paragraph-and-list", "intro\n\n- one\n- two\n- three\n"),
    ("numbered-list", "1. one\n2. two\n3. three\n"),
    (
        "fenced-code",
        "```python\nprint(1)\n```\n",
    ),
    (
        "indented-code",
        "    line 1\n    line 2\n",
    ),
    ("blockquote", "> quoted text\n> continues here\n"),
    ("hr-dashes", "---\n"),
    ("link-reference", "[label]: https://example.com \"Title\"\n"),
    (
        "gfm-table",
        "| h1 | h2 |\n| -- | -- |\n| a  | b  |\n",
    ),
    ("front-matter", "---\ntitle: hello\n---\n\n# body\n"),
    (
        "inline-styles",
        "**bold** *italic* `code` [link](url) ![img](src)\n",
    ),
    (
        "html-block",
        "<div>\n  raw html\n</div>\n",
    ),
    (
        "mixed-prose-code-quote",
        "# Intro\n\ntext\n\n```\ncode\n```\n\n> quote\n\n- list\n",
    ),
)


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


class TestRoundTripFixtures:
    @pytest.mark.parametrize(("label", "source"), _EDGE_CASES, ids=[case[0] for case in _EDGE_CASES])
    def test_edge_case_round_trips(self, backend: MarkdownStructuralLanguage, label: str, source: str) -> None:
        assert_round_trip(backend, label, source)


# -----------------------------------------------------------------------------
# Parse errors
# -----------------------------------------------------------------------------


class TestParseErrors:
    def test_markdown_accepts_all_inputs(self, backend: MarkdownStructuralLanguage) -> None:
        # markdown-it-py is extremely forgiving; even broken-looking source parses cleanly
        weird = "###### h6 with trailing **unclosed emphasis\n\n`unterminated code\n"
        tree = backend.parse(weird)
        assert backend.serialize(tree) == weird


# -----------------------------------------------------------------------------
# Kind schema
# -----------------------------------------------------------------------------


class TestKindSchema:
    def test_language_key_and_source_kinds(self, backend: MarkdownStructuralLanguage) -> None:
        schema = backend.kind_schema
        assert schema.language_key == "markdown"
        assert schema.source_kinds == frozenset({"source_file"})

    def test_expected_kinds_present(self, backend: MarkdownStructuralLanguage) -> None:
        schema = backend.kind_schema
        expected = {
            "source_file",
            "heading",
            "paragraph",
            "list",
            "list_item",
            "code_block",
            "quote_block",
            "hr",
            "link_ref",
            "table",
            "html_block",
            "link",
            "image",
        }
        assert expected <= set(schema.kinds)

    def test_list_item_only_allowed_under_list(self) -> None:
        schema = markdown_kind_schema()
        list_item = schema.get("list_item")
        assert list_item.allowed_parent_kinds == frozenset({"list"})

    def test_heading_only_allowed_under_source_file(self) -> None:
        schema = markdown_kind_schema()
        heading = schema.get("heading")
        assert heading.allowed_parent_kinds == frozenset({"source_file"})

    def test_validate_composition_rules(self) -> None:
        schema = markdown_kind_schema()
        # list_item at document root is rejected
        with pytest.raises(DeclarationError):
            schema.validate_composition("source_file", "list_item")
        # list_item inside list is fine
        schema.validate_composition("list", "list_item")
        # heading at document root is fine
        schema.validate_composition("source_file", "heading")
        # heading inside a list is rejected
        with pytest.raises(DeclarationError):
            schema.validate_composition("list", "heading")


# -----------------------------------------------------------------------------
# Logical name resolver
# -----------------------------------------------------------------------------


class TestLogicalNameResolver:
    def test_parse_slash_form(self, tmp_path: Path) -> None:
        resolver = MarkdownLogicalNameResolver(tmp_path)
        name = resolver.parse("docs/intro/overview")
        assert name.parts == ("docs", "intro", "overview")

    def test_parse_dotted_form(self, tmp_path: Path) -> None:
        resolver = MarkdownLogicalNameResolver(tmp_path)
        name = resolver.parse("docs.intro.overview")
        assert name.parts == ("docs", "intro", "overview")

    @pytest.mark.parametrize("invalid", ["", "foo/", "/foo", "a..b", "-bad"])
    def test_parse_rejects_invalid(self, tmp_path: Path, invalid: str) -> None:
        resolver = MarkdownLogicalNameResolver(tmp_path)
        with pytest.raises(NameResolutionError):
            resolver.parse(invalid)

    def test_resolve_existing_md(self, tmp_path: Path) -> None:
        root = tmp_path / "docs"
        (root / "intro").mkdir(parents=True)
        (root / "intro" / "overview.md").write_text("")
        resolver = MarkdownLogicalNameResolver(tmp_path, source_roots=[root])
        resolution = resolver.resolve(resolver.parse("intro/overview"))
        assert resolution.exists is True
        assert resolution.relative_path == "docs/intro/overview.md"
        assert resolution.source_kind == "source_file"

    def test_resolve_prefers_md_over_markdown(self, tmp_path: Path) -> None:
        root = tmp_path / "docs"
        (root / "page").mkdir(parents=True, exist_ok=True)
        (root / "page.md").write_text("")
        (root / "page.markdown").write_text("")
        resolver = MarkdownLogicalNameResolver(tmp_path, source_roots=[root])
        resolution = resolver.resolve(resolver.parse("page"))
        assert resolution.relative_path == "docs/page.md"

    def test_resolve_nonexistent_synthesizes(self, tmp_path: Path) -> None:
        root = tmp_path / "docs"
        root.mkdir()
        resolver = MarkdownLogicalNameResolver(tmp_path, source_roots=[root])
        resolution = resolver.resolve(resolver.parse("new/page"))
        assert resolution.exists is False
        assert resolution.relative_path == "docs/new/page.md"


# -----------------------------------------------------------------------------
# Symbol walk
# -----------------------------------------------------------------------------


class TestWalkSymbols:
    def test_walks_headings_as_named_symbols(self, backend: MarkdownStructuralLanguage) -> None:
        source = (
            "# Intro\n\n"
            "body of intro\n\n"
            "## Motivation\n\n"
            "why\n\n"
            "## Details\n\n"
            "what\n\n"
            "# Part Two\n\n"
            "end\n"
        )
        tree = backend.parse(source)
        symbols = [(path, kind) for path, kind, _ in backend.walk_symbols(tree)]
        assert ("Intro", "heading") in symbols
        assert ("Intro/Motivation", "heading") in symbols
        assert ("Intro/Details", "heading") in symbols
        assert ("Part Two", "heading") in symbols

    def test_deep_heading_hierarchy(self, backend: MarkdownStructuralLanguage) -> None:
        source = "# A\n\n## B\n\n### C\n\n#### D\n\n### E\n\n## F\n\n# G\n"
        tree = backend.parse(source)
        paths = [p for p, _k, _r in backend.walk_symbols(tree)]
        assert paths == ["A", "A/B", "A/B/C", "A/B/C/D", "A/B/E", "A/F", "G"]

    def test_heading_body_range_extends_to_next_peer(self, backend: MarkdownStructuralLanguage) -> None:
        source = "# A\n\ninside A\n\n# B\n\ninside B\n"
        tree = backend.parse(source)
        by_path = {p: r for p, _k, r in backend.walk_symbols(tree)}
        a_ref = by_path["A"]
        assert a_ref.body_range is not None
        start, end = a_ref.body_range
        inside = source[start:end]
        assert "inside A" in inside
        assert "# B" not in inside  # scope ends before the next level-1 heading

    def test_walk_empty_source_yields_nothing(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("")
        assert list(backend.walk_symbols(tree)) == []

    def test_paragraph_without_heading_yields_no_symbols(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("just a paragraph\n")
        assert list(backend.walk_symbols(tree)) == []

    def test_root_kind_is_source_file(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("")
        assert backend.root_kind(tree) == "source_file"


# -----------------------------------------------------------------------------
# Declaration + mutation
# -----------------------------------------------------------------------------


class TestDeclarationAndMutation:
    def test_build_heading(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration("heading", {"text": "Title", "level": 2}, ())
        assert decl.source == "## Title\n"

    def test_heading_level_out_of_range_rejected(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("heading", {"text": "x", "level": 7}, ())
        with pytest.raises(DeclarationError):
            backend.build_declaration("heading", {"text": "x", "level": 0}, ())

    def test_heading_text_with_newline_rejected(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("heading", {"text": "a\nb", "level": 1}, ())

    def test_build_paragraph(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration("paragraph", {"content": "hello world"}, ())
        assert decl.source == "hello world\n"

    def test_build_fenced_code_block(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "code_block",
            {"content": "print(1)\n", "language": "python"},
            (),
        )
        assert decl.source == "```python\nprint(1)\n```\n"

    def test_build_indented_code_block(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration("code_block", {"content": "abc\nxyz"}, ())
        assert "    abc" in decl.source
        assert "    xyz" in decl.source

    def test_build_list_bullet(self, backend: MarkdownStructuralLanguage) -> None:
        item_a = backend.build_declaration("list_item", {"body": "first"}, ())
        item_b = backend.build_declaration("list_item", {"body": "second"}, ())
        decl = backend.build_declaration("list", {"ordered": False}, (item_a, item_b))
        assert "- first" in decl.source
        assert "- second" in decl.source

    def test_build_list_ordered(self, backend: MarkdownStructuralLanguage) -> None:
        items = tuple(backend.build_declaration("list_item", {"body": f"item {i}"}, ()) for i in range(3))
        decl = backend.build_declaration("list", {"ordered": True}, items)
        assert "1. item 0" in decl.source
        assert "2. item 1" in decl.source
        assert "3. item 2" in decl.source

    def test_build_list_rejects_empty(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("list", {"ordered": False}, ())

    def test_build_list_rejects_non_list_item_child(self, backend: MarkdownStructuralLanguage) -> None:
        heading = backend.build_declaration("heading", {"text": "oops", "level": 1}, ())
        with pytest.raises(DeclarationError):
            backend.build_declaration("list", {"ordered": False}, (heading,))

    def test_build_hr(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration("hr", {}, ())
        assert decl.source == "---\n"

    def test_build_link_ref_with_title(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "link_ref",
            {"label": "serena", "destination": "https://example.com", "title": "Example"},
            (),
        )
        assert decl.source == '[serena]: https://example.com "Example"\n'

    def test_build_link_ref_without_title(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration(
            "link_ref",
            {"label": "x", "destination": "https://x"},
            (),
        )
        assert decl.source == "[x]: https://x\n"

    def test_build_quote_block(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.build_declaration("quote_block", {"content": "one\ntwo"}, ())
        assert decl.source == "> one\n> two\n"

    def test_build_declaration_missing_required_raises(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("heading", {"text": "x"}, ())  # missing level
        with pytest.raises(DeclarationError):
            backend.build_declaration("paragraph", {}, ())  # missing content

    def test_build_declaration_rejects_wrong_attr_type(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.build_declaration("heading", {"text": "x", "level": "2"}, ())  # level must be int
        with pytest.raises(DeclarationError):
            backend.build_declaration("list", {"ordered": "yes"}, (
                backend.build_declaration("list_item", {"body": "x"}, ()),
            ))

    def test_insert_child_at_end(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# A\n\nbody\n")
        decl = backend.build_declaration("paragraph", {"content": "new"}, ())
        new_tree = backend.insert_child(tree, decl, anchor=None, position="end")
        assert "new\n" in backend.serialize(new_tree)
        # original tree unchanged
        assert "new" not in backend.serialize(tree)

    def test_insert_child_before_heading_anchor(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# A\n\nbody\n\n# B\n\nend\n")
        by_path = {p: r for p, _k, r in backend.walk_symbols(tree)}
        b_ref = by_path["B"]
        decl = backend.build_declaration("hr", {}, ())
        new_tree = backend.insert_child(tree, decl, anchor=b_ref, position="before")
        serialized = backend.serialize(new_tree)
        assert serialized.index("---") > serialized.index("# A")
        assert serialized.index("---") < serialized.index("# B")

    def test_insert_child_after_heading_anchor(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# A\n\nbody\n")
        by_path = {p: r for p, _k, r in backend.walk_symbols(tree)}
        a_ref = by_path["A"]
        decl = backend.build_declaration("paragraph", {"content": "added"}, ())
        new_tree = backend.insert_child(tree, decl, anchor=a_ref, position="after")
        serialized = backend.serialize(new_tree)
        assert "# A" in serialized
        assert "added" in serialized
        assert serialized.index("# A") < serialized.index("added")

    def test_insert_child_into_heading_scope(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# A\n\nexisting\n\n# B\n\nend\n")
        by_path = {p: r for p, _k, r in backend.walk_symbols(tree)}
        a_ref = by_path["A"]
        decl = backend.build_declaration("paragraph", {"content": "new in A"}, ())
        new_tree = backend.insert_child(tree, decl, anchor=a_ref, position="end")
        serialized = backend.serialize(new_tree)
        # new content lies inside A's scope, before B
        new_offset = serialized.index("new in A")
        b_offset = serialized.index("# B")
        assert new_offset < b_offset

    def test_insert_child_at_document_start(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# A\n\nbody\n")
        decl = backend.build_declaration("heading", {"text": "Preface", "level": 1}, ())
        new_tree = backend.insert_child(tree, decl, anchor=None, position="start")
        serialized = backend.serialize(new_tree)
        assert serialized.startswith("# Preface")
        assert serialized.index("# Preface") < serialized.index("# A")

    def test_insert_child_into_empty_source(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.empty_source("source_file")
        decl = backend.build_declaration("heading", {"text": "Start", "level": 1}, ())
        new_tree = backend.insert_child(tree, decl, anchor=None, position="end")
        assert backend.serialize(new_tree) == "# Start\n"

    def test_remove_heading_removes_heading_line(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# A\n\nbody\n")
        by_path = {p: r for p, _k, r in backend.walk_symbols(tree)}
        a_ref = by_path["A"]
        new_tree = backend.remove_child(tree, a_ref)
        serialized = backend.serialize(new_tree)
        assert "# A" not in serialized
        assert "body" in serialized

    def test_insert_before_without_anchor_rejected(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("")
        decl = backend.build_declaration("hr", {}, ())
        with pytest.raises(ValueError):
            backend.insert_child(tree, decl, anchor=None, position="before")


# -----------------------------------------------------------------------------
# Pattern matching
# -----------------------------------------------------------------------------


class TestPatternMatching:
    def test_empty_pattern_rejected(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.compile_pattern("")

    def test_compile_returns_usable_pattern(self, backend: MarkdownStructuralLanguage) -> None:
        pattern = backend.compile_pattern("$x")
        assert pattern.placeholders  # at least one placeholder was encoded

    def test_exact_pattern_matches_paragraph(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# H\n\nhello world\n")
        pattern = backend.compile_pattern("hello world")
        matches = list(backend.find_matches(tree, pattern))
        assert len(matches) == 1
        assert "hello world" in matches[0].node.name_path

    def test_whole_text_capture_binds(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("first para\n\nsecond para\n")
        pattern = backend.compile_pattern("$p")
        matches = list(backend.find_matches(tree, pattern))
        # at least one paragraph should capture its text into $p
        captured = [m.bindings.get("p") for m in matches if "p" in m.bindings]
        assert any("first para" in c for c in captured)

    def test_no_match_yields_empty(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# H\n\nhello\n")
        pattern = backend.compile_pattern("completely unmatched content")
        assert list(backend.find_matches(tree, pattern)) == []

    def test_find_matches_does_not_raise(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# H\n\nfoo bar baz\n")
        pattern = backend.compile_pattern("foo $x baz")
        # we don't require this case to land on the right node, just that it completes
        list(backend.find_matches(tree, pattern))


# -----------------------------------------------------------------------------
# Rewriting
# -----------------------------------------------------------------------------


class TestRewriting:
    def test_render_replacement_substitutes_captures(self, backend: MarkdownStructuralLanguage) -> None:
        decl = backend.render_replacement("hello $name", {"name": "Serena"})
        assert decl.source == "hello Serena"

    def test_render_missing_binding_raises(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("$missing", {})

    def test_render_wildcard_rejected(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(PatternError):
            backend.render_replacement("$_", {})

    def test_apply_replacement_replaces_match_region(self, backend: MarkdownStructuralLanguage) -> None:
        tree = backend.parse("# H\n\nold\n")
        pattern = backend.compile_pattern("old")
        matches = list(backend.find_matches(tree, pattern))
        assert matches, "expected at least one match"
        replacement = backend.render_replacement("new", {})
        new_tree = backend.apply_replacement(tree, matches[0], replacement)
        assert "new" in backend.serialize(new_tree)
        assert "old" not in backend.serialize(new_tree)


# -----------------------------------------------------------------------------
# empty_source
# -----------------------------------------------------------------------------


class TestEmptySource:
    def test_empty_source_round_trips(self, backend: MarkdownStructuralLanguage) -> None:
        empty = backend.empty_source("source_file")
        assert backend.serialize(empty) == ""

    def test_empty_source_rejects_unknown_kind(self, backend: MarkdownStructuralLanguage) -> None:
        with pytest.raises(DeclarationError):
            backend.empty_source("heading")
