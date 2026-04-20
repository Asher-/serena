"""Markdown Language Server backed by the M4 :class:`MarkdownStructuralLanguage`.

A minimal LSP implementation for markdown documents. Provides hierarchical
document symbols derived from the same markdown-it-py parse tree that drives
memory-file I/O in Serena, so the cursor surface and the structural-edit
surface agree on headings byte-for-byte.

The v2 surface adds workspace-wide cross-file linking on top of the v1
``documentSymbol`` foundation:

* ``textDocument/references`` — a cursor on a heading returns every
  workspace location of an inline link ``[text](file.md#slug)`` or
  ``[text](#slug)`` whose fragment resolves to that heading's slug.
* ``textDocument/definition`` — a cursor on an inline link returns the
  heading location its fragment resolves to.

Slug algorithm
--------------

Slugs are derived from a heading's rendered ``inline.content`` (markdown
delimiters preserved verbatim, then sanitized). The transformation is
intentionally divergent from Marksman's GitHub-flavored algorithm: Serena's
form has *no Unicode normalization* and *no duplicate-suffix dedup*.

* lowercase the text;
* replace each run of one-or-more characters that are not in the set
  ``[a-z0-9-]`` with a single ``-``;
* strip any leading and trailing ``-``.

Examples (``raw heading text → slug``):

* ``Hello, World!`` → ``hello-world``
* ``Foo & Bar``     → ``foo-bar``
* ``Foo / Bar``     → ``foo-bar``
* ``*Foo*``         → ``foo``  (asterisks become hyphens, then strip)

Duplicate slugs across multiple headings resolve to the first hit by
document order; this is a known v2 limitation.

Launched as a subprocess by :class:`SerenaMarkdownLanguageServer` and
communicates via stdio. The backend is stateless — each request re-parses
the document source held by :mod:`pygls`' workspace.
"""

import bisect
import logging
import os
import pathlib
import re
from typing import cast

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from solidlsp.structural.backends.markdown import MarkdownStructuralLanguage, _MdTree

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server = LanguageServer("serena-markdown-lsp", "0.1.0")
_backend = MarkdownStructuralLanguage()


# ---------------------------------------------------------------------------
# Position / offset helpers
# ---------------------------------------------------------------------------


def _offset_to_position(line_starts: tuple[int, ...], offset: int) -> lsp.Position:
    # locate the line containing ``offset`` via binary search on line_starts
    idx = bisect.bisect_right(line_starts, offset) - 1
    idx = max(0, min(idx, len(line_starts) - 1))

    # compute the column as the offset delta from that line's start
    character = offset - line_starts[idx]
    return lsp.Position(line=idx, character=character)


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


_SLUG_NON_PERMITTED_RE = re.compile(r"[^a-z0-9\-]+")


def _slug(text: str) -> str:
    """Compute the slug for a heading's rendered text.

    See the module docstring for the full algorithm and divergence from
    Marksman.

    :param text: the heading's ``inline.content`` (no surrounding ``#`` markers).
    :return: the URL-fragment-friendly slug, possibly empty when the input
        contains no permitted characters.
    """
    # lowercase first so the character class only needs to know about lower-case letters
    lowered = text.lower()

    # collapse every disallowed run into a single hyphen, then trim hyphen padding
    collapsed = _SLUG_NON_PERMITTED_RE.sub("-", lowered)
    return collapsed.strip("-")


# ---------------------------------------------------------------------------
# Heading and link extraction
# ---------------------------------------------------------------------------


_INLINE_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\n]*)\)")


def _extract_headings(tree: _MdTree) -> list[tuple[str, lsp.Range, lsp.Range]]:
    """Return ``(slug, full_range, selection_range)`` for every heading in document order.

    ``full_range`` covers the heading's scope (heading line + body lines until
    the next equal-or-shallower heading), matching the document-symbol range.
    ``selection_range`` covers just the heading line, matching the document-symbol
    selection range.
    """
    # walk_symbols yields refs in document order; we mirror its scope ranges here
    line_starts = tree.line_starts
    out: list[tuple[str, lsp.Range, lsp.Range]] = []
    for _path, _kind, ref in _backend.walk_symbols(tree):
        # the leaf segment of the name path is the rendered inline content
        leaf_text = ref.name_path.split("/")[-1]
        slug = _slug(leaf_text)

        # full_range mirrors documentSymbol's range, ending at the body's end (or extent end)
        extent_start = ref.extent_offset
        extent_end = ref.extent_offset + ref.extent_length
        body_end = ref.body_range[1] if ref.body_range is not None else extent_end
        full_range = lsp.Range(
            start=_offset_to_position(line_starts, extent_start),
            end=_offset_to_position(line_starts, body_end),
        )
        selection_range = lsp.Range(
            start=_offset_to_position(line_starts, extent_start),
            end=_offset_to_position(line_starts, extent_end),
        )
        out.append((slug, full_range, selection_range))
    return out


def _extract_inline_links(tree: _MdTree) -> list[tuple[lsp.Range, str]]:
    """Return ``(range, href)`` for every inline ``[text](href)`` link in the document.

    Walks the markdown-it inline tokens to count links per inline range, then
    uses a regex over the matching source slice to recover positional info.
    Reference-style links and autolinks are not surfaced here — only the
    ``[text](dest)`` form, which is the form the slug-anchor protocol uses.
    """
    line_starts = tree.line_starts
    out: list[tuple[lsp.Range, str]] = []

    # collect inline tokens with their byte ranges so the regex can scan slices
    for tok in tree.tokens:
        if tok.type != "inline" or tok.map is None or tok.children is None:
            continue
        # count the number of inline links this token contributes; if zero, skip
        link_open_count = sum(1 for child in tok.children if child.type == "link_open")
        if link_open_count == 0:
            continue

        # compute the byte range of the inline token using its line map
        start_line, end_line = tok.map
        start_offset = line_starts[min(start_line, len(line_starts) - 1)]
        end_offset = line_starts[min(end_line, len(line_starts) - 1)]
        slice_text = tree.source[start_offset:end_offset]

        # scan for inline link occurrences within the slice; the link_open count is the upper bound
        matches = list(_INLINE_LINK_RE.finditer(slice_text))
        if len(matches) < link_open_count:
            # extremely defensive: a parser quirk may have eaten a link the regex cannot see
            continue

        # take the first ``link_open_count`` matches in document order
        for match in matches[:link_open_count]:
            link_start_offset = start_offset + match.start()
            link_end_offset = start_offset + match.end()
            href = match.group(2)
            link_range = lsp.Range(
                start=_offset_to_position(line_starts, link_start_offset),
                end=_offset_to_position(line_starts, link_end_offset),
            )
            out.append((link_range, href))
    return out


def _parse_link_target(href: str) -> tuple[str | None, str | None]:
    """Split a markdown link ``href`` into ``(file_part, fragment)``.

    ``file_part`` is the text before ``#`` (``None`` if the link is a bare
    fragment ``#slug``). ``fragment`` is the text after ``#`` (``None`` if
    the link has no fragment).
    """
    # split once on the first ``#``; everything after is the fragment
    if "#" in href:
        file_part, _, fragment = href.partition("#")
        return (file_part or None, fragment or None)
    return (href or None, None)


def _range_contains(rng: lsp.Range, position: lsp.Position) -> bool:
    """Return True iff ``position`` falls within ``rng`` (inclusive on both ends)."""
    # compare line then character; inclusive end-character matches the way clients hover over links
    if position.line < rng.start.line or position.line > rng.end.line:
        return False
    if position.line == rng.start.line and position.character < rng.start.character:
        return False
    if position.line == rng.end.line and position.character > rng.end.character:
        return False
    return True


# ---------------------------------------------------------------------------
# Workspace scanning
# ---------------------------------------------------------------------------


_MARKDOWN_EXTENSIONS = (".md", ".markdown")


def _get_workspace_roots() -> list[str]:
    """Return absolute paths of all workspace roots known to the LSP.

    After ``initialize``, pygls populates ``server.workspace`` with the
    root URI/path and any workspace folders sent by the client.
    """
    # any error reaching the workspace before initialization should be silent — the request just returns nothing
    roots: list[str] = []
    try:
        ws = server.workspace
    except (RuntimeError, AttributeError):
        return roots

    # workspace.folders is a dict of {uri_string: WorkspaceFolder}
    if hasattr(ws, "folders") and ws.folders:
        for folder_uri in ws.folders:
            fs_path = to_fs_path(folder_uri)
            if fs_path:
                # resolve() normalizes drive letter casing on Windows
                resolved = str(pathlib.Path(fs_path).resolve())
                if os.path.isdir(resolved):
                    roots.append(resolved)

    # fall back to root_path when no folders were sent
    if not roots and hasattr(ws, "root_path") and ws.root_path:
        resolved = str(pathlib.Path(ws.root_path).resolve())
        if os.path.isdir(resolved):
            roots.append(resolved)

    return roots


def _path_to_uri(path: str) -> str:
    """Convert a filesystem path to a ``file://`` URI."""
    return pathlib.Path(path).as_uri()


def _get_all_md_files() -> list[tuple[str, str, str]]:
    """Scan workspace roots for all markdown files, preferring open documents.

    :return: list of ``(uri, file_path, source_text)`` tuples. Documents
        already open in the LSP workspace are read from the live buffer
        (which may differ from disk during an unsaved edit); the rest are
        read from disk.
    """
    results: list[tuple[str, str, str]] = []
    seen_paths: set[str] = set()

    # first include all open documents; their source may be ahead of disk
    try:
        for uri, doc in server.workspace.text_documents.items():
            if not any(uri.endswith(ext) for ext in _MARKDOWN_EXTENSIONS):
                continue
            file_path = to_fs_path(uri) or uri
            norm = os.path.normcase(os.path.normpath(file_path))
            seen_paths.add(norm)
            results.append((uri, file_path, doc.source))
    except (RuntimeError, AttributeError):
        pass

    # then walk each workspace root for any markdown files not already open
    for root in _get_workspace_roots():
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                if not any(fname.endswith(ext) for ext in _MARKDOWN_EXTENSIONS):
                    continue
                full_path = os.path.join(dirpath, fname)
                norm = os.path.normcase(os.path.normpath(full_path))
                if norm in seen_paths:
                    continue
                seen_paths.add(norm)
                try:
                    with open(full_path, encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError:
                    continue
                uri = _path_to_uri(full_path)
                results.append((uri, full_path, source))

    return results


# ---------------------------------------------------------------------------
# Document symbol
# ---------------------------------------------------------------------------


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
            f"heading count mismatch: walk_symbols yielded {len(refs)} refs, tokens yielded {len(levels)} heading_open entries"
        )

    # build a flat (level, DocumentSymbol) list; range covers the scope, selection_range covers the heading line only
    line_starts = tree.line_starts
    flat: list[tuple[int, lsp.DocumentSymbol]] = []
    for level, ref in zip(levels, refs, strict=False):
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


# ---------------------------------------------------------------------------
# Cursor → heading / link resolution
# ---------------------------------------------------------------------------


def _heading_at_position(tree: _MdTree, position: lsp.Position) -> tuple[str, lsp.Range] | None:
    """Return the slug + selection range for the heading whose line contains ``position``.

    A position only counts as "on a heading" when it sits on the heading's
    own line (the selection range), not on its body. This matches editor
    behavior for ``find references``.
    """
    # iterate document order and return the first match — headings cannot overlap on a single line
    for slug, _full_range, selection_range in _extract_headings(tree):
        if selection_range.start.line == position.line:
            return slug, selection_range
    return None


def _link_at_position(tree: _MdTree, position: lsp.Position) -> tuple[str, lsp.Range] | None:
    """Return the href + range for the inline link that contains ``position``."""
    # iterate every link; ranges are non-overlapping for valid markdown
    for link_range, href in _extract_inline_links(tree):
        if _range_contains(link_range, position):
            return href, link_range
    return None


def _resolve_target_uri(source_uri: str, file_part: str | None) -> str | None:
    """Resolve a link's file part to an absolute ``file://`` URI.

    :param source_uri: URI of the document containing the link (used to
        resolve relative paths).
    :param file_part: the text before the ``#`` in the link's ``href``;
        ``None`` for bare fragments (link resolves to the source document).
    :return: absolute ``file://`` URI of the target document, or ``None`` if
        the source URI can't be converted to a filesystem path.
    """
    # convert the source uri to a filesystem path so we can resolve relative file refs
    source_fs = to_fs_path(source_uri)
    if source_fs is None:
        return None

    # bare fragment links target the same document
    if file_part is None or file_part == "":
        return _path_to_uri(source_fs)

    # join relative paths against the source file's directory; absolute paths win
    candidate = pathlib.Path(file_part)
    if not candidate.is_absolute():
        candidate = pathlib.Path(source_fs).parent / candidate
    return _path_to_uri(str(candidate.resolve()))


# ---------------------------------------------------------------------------
# Pygls handlers
# ---------------------------------------------------------------------------


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


@server.feature(lsp.TEXT_DOCUMENT_REFERENCES)
def references(params: lsp.ReferenceParams) -> list[lsp.Location]:
    """Find every inline link in the workspace whose fragment resolves to the heading at the cursor.

    Steps:

    1. parse the source document, identify the heading the cursor sits on,
       and capture its slug + URI;
    2. scan every workspace markdown file, parse it, and gather all
       ``[text](href)`` inline links;
    3. for each link, resolve ``href`` to ``(target_uri, fragment)`` —
       relative paths are joined against the link-bearing document;
    4. yield a :class:`Location` for every link whose target file matches
       the source heading's URI and whose fragment slug matches the
       heading's slug.

    When ``params.context.include_declaration`` is true, the heading's own
    selection range is appended to the result.
    """
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        tree = _backend.parse(doc.source)
    except Exception as err:
        logger.error(f"Error in references (parse source): {err}")
        return []

    # short-circuit when the cursor is not on a heading line
    heading = _heading_at_position(tree, params.position)
    if heading is None:
        return []
    target_slug, target_selection = heading

    # canonicalize the source URI so cross-file comparisons are exact
    source_path = to_fs_path(params.text_document.uri)
    if source_path is None:
        return []
    source_uri_norm = _path_to_uri(str(pathlib.Path(source_path).resolve()))

    results: list[lsp.Location] = []

    # walk the workspace once; build per-file link inventories on the fly
    for link_uri, link_path, link_source in _get_all_md_files():
        try:
            link_tree = _backend.parse(link_source)
        except Exception as err:
            logger.error(f"Error parsing {link_path}: {err}")
            continue

        for link_range, href in _extract_inline_links(link_tree):
            file_part, fragment = _parse_link_target(href)
            if fragment is None:
                continue
            if _slug(fragment) != target_slug:
                continue

            # resolve the link's target to a canonical URI for cross-file comparison
            resolved_uri = _resolve_target_uri(link_uri, file_part)
            if resolved_uri is None or resolved_uri != source_uri_norm:
                continue
            results.append(lsp.Location(uri=link_uri, range=link_range))

    # honor include_declaration by appending the heading's own selection range
    include_decl = bool(params.context and params.context.include_declaration)
    if include_decl:
        results.append(lsp.Location(uri=source_uri_norm, range=target_selection))

    return results


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def definition(params: lsp.DefinitionParams) -> list[lsp.Location]:
    """Resolve an inline link's fragment to the heading location it points at.

    Steps:

    1. parse the source document and locate the link the cursor sits in;
    2. split the link's ``href`` into ``(file_part, fragment)`` — return
       no results when the link has no fragment to anchor to;
    3. resolve the file part to a target URI (relative paths join against
       the source document's directory);
    4. parse the target document and return the first heading whose slug
       matches the link's fragment slug.
    """
    try:
        doc = server.workspace.get_text_document(params.text_document.uri)
        tree = _backend.parse(doc.source)
    except Exception as err:
        logger.error(f"Error in definition (parse source): {err}")
        return []

    # short-circuit when the cursor is not inside an inline link
    link = _link_at_position(tree, params.position)
    if link is None:
        return []
    href, _link_range = link
    file_part, fragment = _parse_link_target(href)
    if fragment is None:
        return []

    # resolve the link target file; bare fragments stay in the source document
    target_uri = _resolve_target_uri(params.text_document.uri, file_part)
    if target_uri is None:
        return []

    # load and parse the target; prefer an open buffer over the on-disk source
    target_source = _read_target_source(target_uri)
    if target_source is None:
        return []
    try:
        target_tree = _backend.parse(target_source)
    except Exception as err:
        logger.error(f"Error parsing target {target_uri}: {err}")
        return []

    # return the first matching heading in document order; v2 deliberately ignores duplicate-slug dedup
    target_slug = _slug(fragment)
    for slug, _full_range, selection_range in _extract_headings(target_tree):
        if slug == target_slug:
            return [lsp.Location(uri=target_uri, range=selection_range)]
    return []


def _read_target_source(target_uri: str) -> str | None:
    """Read a target document's source, preferring an open buffer over disk."""
    # check the workspace for an open document first; saves a disk hit and
    # respects unsaved edits
    try:
        for uri, doc in server.workspace.text_documents.items():
            if uri == target_uri:
                return doc.source
    except (RuntimeError, AttributeError):
        pass

    # fall back to disk for files not currently open
    target_path = to_fs_path(target_uri)
    if target_path is None or not os.path.isfile(target_path):
        return None
    try:
        with open(target_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


if __name__ == "__main__":
    server.start_io()
