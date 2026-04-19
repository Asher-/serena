"""
Structural-parity tests for the Serena M4-backed Markdown language server.

These tests assert the invariant that motivates the Serena markdown server's
existence: the LSP symbol surface and the :class:`MarkdownStructuralLanguage`
surface must agree on heading discovery, since both share a single
markdown-it-py parse. If they diverge, it is a bug — not a parametric
difference between two independent parsers.
"""

from pathlib import Path

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language
from solidlsp.structural.backends.markdown import MarkdownStructuralLanguage


def _flatten_lsp_symbols(symbols: list[dict]) -> list[tuple[str, int]]:
    """Flatten a hierarchical UnifiedSymbolInformation tree into ``(name, start_line)`` pairs.

    :param symbols: root-level document symbols from
        :meth:`SolidLanguageServer.request_document_symbols`.
    :return: flat list preserving document order; each entry is the heading
        name and its start line (0-based).
    """
    out: list[tuple[str, int]] = []

    # depth-first, children-in-order walk matches the linear heading order in the source
    def walk(nodes: list[dict]) -> None:
        for n in nodes:
            out.append((n["name"], n["location"]["range"]["start"]["line"]))
            for c in n.get("children", []) or []:
                walk([c])

    walk(symbols)
    return out


def _flatten_structural_headings(source: str) -> list[tuple[str, int]]:
    """Walk the M4 backend directly, returning ``(leaf_heading_text, start_line)`` pairs."""
    backend = MarkdownStructuralLanguage()
    tree = backend.parse(source)

    # compute line number for each heading via the tree's line_starts index
    line_starts = tree.line_starts
    import bisect

    out: list[tuple[str, int]] = []
    for name_path, _kind, ref in backend.walk_symbols(tree):
        leaf = name_path.split("/")[-1]
        # last line_starts entry <= extent_offset gives the heading's 0-based line
        idx = bisect.bisect_right(line_starts, ref.extent_offset) - 1
        out.append((leaf, max(0, idx)))
    return out


@pytest.mark.markdown
class TestMarkdownM4StructuralParity:
    """Asserts the LSP surface and the M4 backend agree on every heading."""

    @pytest.mark.parametrize("language_server", [Language.MARKDOWN], indirect=True)
    @pytest.mark.parametrize("relative_path", ["README.md", "guide.md", "api.md"])
    def test_lsp_matches_structural_backend(
        self, language_server: SolidLanguageServer, relative_path: str
    ) -> None:
        """Headings from the LSP match those from :class:`MarkdownStructuralLanguage` one-to-one."""
        # read the source through the LSP's workspace-rooted path so both surfaces see identical bytes
        repo_root = Path(language_server.repository_root_path)
        source = (repo_root / relative_path).read_text(encoding="utf-8")

        # gather the LSP view
        all_symbols, _roots = language_server.request_document_symbols(relative_path).get_all_symbols_and_roots()
        lsp_view = [
            (sym["name"], sym["location"]["range"]["start"]["line"]) for sym in all_symbols
        ]

        # gather the structural backend view
        structural_view = _flatten_structural_headings(source)

        # parity: same count, same names, same starting lines, same order
        assert lsp_view == structural_view, (
            f"LSP/structural disagreement on {relative_path}:\n"
            f"  LSP:        {lsp_view}\n"
            f"  Structural: {structural_view}"
        )


@pytest.mark.markdown
class TestMarkdownM4HierarchyShape:
    """Validates the hierarchy the M4 server reports for nested headings."""

    @pytest.mark.parametrize("language_server", [Language.MARKDOWN], indirect=True)
    def test_readme_hierarchy(self, language_server: SolidLanguageServer) -> None:
        """README.md's h1 contains its h2 siblings as children, and each h2 contains its h3 descendants."""
        _all, roots = language_server.request_document_symbols("README.md").get_all_symbols_and_roots()

        # exactly one h1 root: "Test Repository"
        assert len(roots) == 1, f"expected a single h1 root; got {[r['name'] for r in roots]}"
        root = roots[0]
        assert root["name"] == "Test Repository"

        # h2 children, in document order
        h2_names = [c["name"] for c in root.get("children", []) or []]
        assert h2_names == ["Overview", "Features", "Code Examples", "References", "License"], h2_names

        # the "Features" h2 contains two h3 children
        features = next(c for c in root["children"] if c["name"] == "Features")
        h3_names = [c["name"] for c in features.get("children", []) or []]
        assert h3_names == ["Installation", "Usage"], h3_names
