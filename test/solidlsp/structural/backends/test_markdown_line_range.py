"""Tests for the line span the markdown backend reports for a walked node."""

from solidlsp.structural.backends.markdown import MarkdownStructuralLanguage


class TestNodeLineRange:
    def test_heading_spans_its_declaration_line(self) -> None:
        backend = MarkdownStructuralLanguage()
        nodes = {path: node for path, _kind, node in backend.walk_symbols(backend.parse("# Title\n\nalpha\n\n## Second\n\nbeta\n"))}

        assert backend.node_line_range(nodes["Title/Second"]) == (4, 4)

    def test_a_node_that_is_no_heading_has_no_span(self) -> None:
        backend = MarkdownStructuralLanguage()

        assert backend.node_line_range(backend.parse("# Title\n")) is None
