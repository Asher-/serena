"""
v2 Markdown LSP tests: slug normalization, cross-file references, anchor definition.

These tests cover the workspace-wide cross-file linking surface that the v2
``SerenaMarkdownLanguageServer`` adds on top of the v1 ``documentSymbol``
foundation. The slug-normalization tests are pure unit tests against the
private ``_slug`` helper (no LSP boot); the references/definition tests
exercise the live language server against a fixture repo with cross-file
links.
"""

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.language_servers.markdown_lsp_server import _slug
from solidlsp.ls_config import Language

pytestmark = [pytest.mark.markdown]


# ===========================================================================
# Slug normalization (pure unit tests)
# ===========================================================================


class TestSlugNormalization:
    """The slug algorithm matches the documented GitHub-style normalization."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Foo", "foo"),
            ("Hello, World!", "hello-world"),
            ("Foo & Bar", "foo-bar"),
            ("Foo / Bar", "foo-bar"),
            ("*Foo*", "foo"),
            ("   leading spaces", "leading-spaces"),
            ("trailing punctuation!!!", "trailing-punctuation"),
            ("multi   spaces", "multi-spaces"),
            ("UPPER CASE", "upper-case"),
            ("already-hyphenated", "already-hyphenated"),
            ("123 numeric start", "123-numeric-start"),
            ("", ""),
        ],
    )
    def test_slug_normalization(self, raw: str, expected: str) -> None:
        """``_slug`` lowercases, collapses non-[a-z0-9-] runs, and strips edges."""
        assert _slug(raw) == expected


# ===========================================================================
# Cross-file references and anchor definitions (live LSP)
# ===========================================================================


@pytest.mark.parametrize("language_server", [Language.MARKDOWN], indirect=True)
class TestMarkdownM4References:
    """``request_references`` returns inline-link locations targeting a heading."""

    def test_references_to_heading_in_other_file(
        self, language_server: SolidLanguageServer
    ) -> None:
        """A cursor on ``# Foo`` in v2_doc_a.md returns the link in v2_doc_b.md."""
        # the heading "## Foo" lives at line 4 (0-based) in v2_doc_a.md;
        # request_references positions the cursor anywhere on that line
        results = language_server.request_references("v2_doc_a.md", line=4, column=2)

        # at least one link points at this heading from v2_doc_b.md
        rel_paths = sorted({loc["relativePath"] for loc in results})
        assert "v2_doc_b.md" in rel_paths, f"expected v2_doc_b.md in references; got {rel_paths}"

        # verify the matching range covers the [foo anchor](v2_doc_a.md#foo) link text on line 6
        b_locs = [loc for loc in results if loc["relativePath"] == "v2_doc_b.md"]
        assert any(
            loc["range"]["start"]["line"] == 6 for loc in b_locs
        ), f"expected a link on line 6 of v2_doc_b.md; got {b_locs}"

    def test_references_to_second_heading(self, language_server: SolidLanguageServer) -> None:
        """A second-position heading is reachable via its slug from another file."""
        # "## Section Two" lives at line 8 (0-based) in v2_doc_a.md
        results = language_server.request_references("v2_doc_a.md", line=8, column=2)

        # the link text "(v2_doc_a.md#section-two)" in v2_doc_b.md must resolve here
        rel_paths = sorted({loc["relativePath"] for loc in results})
        assert "v2_doc_b.md" in rel_paths, f"expected v2_doc_b.md in references; got {rel_paths}"

    def test_references_off_heading_returns_empty(
        self, language_server: SolidLanguageServer
    ) -> None:
        """A cursor on a body line (not a heading line) returns no references."""
        # line 6 of v2_doc_a.md is body text "Body of the foo section."
        results = language_server.request_references("v2_doc_a.md", line=6, column=2)
        # body lines are not headings; the references handler must short-circuit to []
        assert results == [], f"expected empty references for body cursor; got {results}"


@pytest.mark.parametrize("language_server", [Language.MARKDOWN], indirect=True)
class TestMarkdownM4Definition:
    """``request_definition`` resolves an inline link's fragment to the target heading."""

    def test_definition_of_cross_file_anchor(self, language_server: SolidLanguageServer) -> None:
        """Cursor inside ``[foo anchor](v2_doc_a.md#foo)`` returns the heading in v2_doc_a.md."""
        # the link "(v2_doc_a.md#foo)" lives at line 6 (0-based) of v2_doc_b.md;
        # column 12 sits inside the bracketed text "[foo anchor]"
        results = language_server.request_definition("v2_doc_b.md", line=6, column=12)

        # exactly one heading match; the target is the "## Foo" heading on line 4
        assert len(results) == 1, f"expected one definition; got {results}"
        loc = results[0]
        assert loc["relativePath"] == "v2_doc_a.md"
        assert loc["range"]["start"]["line"] == 4

    def test_definition_of_bare_fragment_link(
        self, language_server: SolidLanguageServer
    ) -> None:
        """Cursor inside ``[self-link](#bare-fragment-link)`` resolves to the same-file heading."""
        # the bare-fragment self-link lives at line 10 (0-based) of v2_doc_b.md
        results = language_server.request_definition("v2_doc_b.md", line=10, column=4)

        # the link's fragment "bare-fragment-link" matches the "## Bare fragment link" heading
        assert len(results) == 1, f"expected one definition; got {results}"
        loc = results[0]
        assert loc["relativePath"] == "v2_doc_b.md"
        # "## Bare fragment link" is the third heading in v2_doc_b.md, on line 8 (0-based)
        assert loc["range"]["start"]["line"] == 8

    def test_definition_off_link_returns_empty(self, language_server: SolidLanguageServer) -> None:
        """Cursor on plain text outside any link returns no definition."""
        # line 0 of v2_doc_b.md is "# Document B" — a heading, not a link
        results = language_server.request_definition("v2_doc_b.md", line=0, column=2)
        # the definition handler short-circuits when the cursor is not in an inline link
        assert results == [], f"expected empty definition for non-link cursor; got {results}"
