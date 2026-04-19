"""Markdown Language Server backed by the M4 :class:`MarkdownStructuralLanguage`.

A minimal LSP implementation for markdown documents. Provides hierarchical
document symbols derived from the same markdown-it-py parse tree that drives
memory-file I/O in Serena, so the cursor surface and the structural-edit
surface agree on headings byte-for-byte.

Launched as a subprocess by :class:`SerenaMarkdownLanguageServer` and
communicates via stdio. The backend is stateless — each
``textDocument/documentSymbol`` request re-parses the document source held
by :mod:`pygls`' workspace.
"""

import bisect
import logging
from typing import cast

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from solidlsp.structural.backends.markdown import MarkdownStructuralLanguage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server = LanguageServer("serena-markdown-lsp", "0.1.0")
_backend = MarkdownStructuralLanguage()


def _offset_to_position(line_starts: tuple[int, ...], offset: int) -> lsp.Position:
    # locate the line containing ``offset`` via binary search on line_starts
    idx = bisect.bisect_right(line_starts, offset) - 1
    idx = max(0, min(idx, len(line_starts) - 1))

    # compute the column as the offset delta from that line's start
    character = offset - line_starts[idx]
    return lsp.Position(line=idx, character=character)


def _build_document_symbols(source: str) -> list[lsp.DocumentSymbol]:
    """Produce a hierarchical :class:`DocumentSymbol` tree for ``source``.

    Uses :meth:`MarkdownStructuralLanguage.walk_symbols` as the authoritative
    source of heading extents and scope ranges. Heading levels are sourced
    from the parallel ``heading_open`` tokens on the same parse tree, so
    the two views are guaranteed to stay in sync — no second parser is
    instantiated.

    :param source: raw markdown source text.
    :return: the root-level :class:`DocumentSymbol` entries, each carrying
        its descendants via the ``children`` field.
    """
    # parse once through the structural backend; the tree carries tokens + line map
    tree = _backend.parse(source)

    # gather refs and matching levels in document order
    refs = [ref for _path, _kind, ref in _backend.walk_symbols(tree)]
    levels = [int(tok.tag[1]) for tok in tree.tokens if tok.type == "heading_open"]
    if len(levels) != len(refs):
        raise RuntimeError(
            f"heading count mismatch: walk_symbols yielded {len(refs)} refs, "
            f"tokens yielded {len(levels)} heading_open entries"
        )

    # build a flat (level, DocumentSymbol) list; range covers the scope, selection_range covers the heading line only
    line_starts = tree.line_starts
    flat: list[tuple[int, lsp.DocumentSymbol]] = []
    for level, ref in zip(levels, refs):
        extent_start = ref.extent_offset
        extent_end = ref.extent_offset + ref.extent_length
        full_end = ref.body_range[1] if ref.body_range is not None else extent_end
        heading_text = ref.name_path.split("/")[-1]

        full_range = lsp.Range(
            start=_offset_to_position(line_starts, extent_start),
            end=_offset_to_position(line_starts, full_end),
        )
        selection_range = lsp.Range(
            start=_offset_to_position(line_starts, extent_start),
            end=_offset_to_position(line_starts, extent_end),
        )
        sym = lsp.DocumentSymbol(
            name=heading_text,
            kind=lsp.SymbolKind.Namespace,
            range=full_range,
            selection_range=selection_range,
            children=[],
        )
        flat.append((level, sym))

    # reconstruct the parent/child hierarchy: a heading's parent is the nearest prior heading with strictly shallower level
    roots: list[lsp.DocumentSymbol] = []
    stack: list[tuple[int, lsp.DocumentSymbol]] = []
    for level, sym in flat:
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            # we populated children=[] at construction time, so the runtime value is always a list
            parent_children = cast(list[lsp.DocumentSymbol], stack[-1][1].children)
            parent_children.append(sym)
        else:
            roots.append(sym)
        stack.append((level, sym))

    return roots


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
    # pygls' workspace tracks document source automatically; no per-server cache needed
    pass


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def did_change(params: lsp.DidChangeTextDocumentParams) -> None:
    # incremental edits are applied by pygls; we re-parse on demand from workspace state
    pass


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def did_close(params: lsp.DidCloseTextDocumentParams) -> None:
    pass


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbol(params: lsp.DocumentSymbolParams) -> list[lsp.DocumentSymbol]:
    """Return the hierarchical heading outline for a markdown document."""
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        return _build_document_symbols(doc.source)
    except Exception as err:
        logger.error(f"Error in document_symbol: {err}")
        return []


if __name__ == "__main__":
    server.start_io()
