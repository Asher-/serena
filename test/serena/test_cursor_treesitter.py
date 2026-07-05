"""Integration tests for the tree-sitter structural rung on the cursor surface (spec-v2 §5.8).

A file no LSP or explicit structural backend claims (``.css`` / ``.sh`` /
``Dockerfile`` / ...) now resolves through the tree-sitter fallback rung -- above
the plaintext floor -- so ``cursor_overview`` yields structural nodes instead of a
"Cannot extract symbols" dead-end, ``cursor_start`` lands a structural cursor whose
anchor carries a 1-based (``cat -n``) line range, and a node's body renders its exact
source bytes. Malformed source never raises: the rung degrades to content, never a
terminal error.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from serena.cursor import CursorManager, ReadRung, StructuralCursorState


def _manager(tmp_path: Path) -> CursorManager:
    """A CursorManager over ``tmp_path`` with a stub project and the default 13-backend registry."""
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
    return CursorManager(project)


def _force_lsp_miss():
    """Patch the LSP retriever so no file analyzes and no symbol resolves -> the ladder falls to
    the structural (tree-sitter) rung.
    """
    retriever = MagicMock()
    retriever.find_unique.side_effect = ValueError("no symbol")
    retriever.can_analyze_file.return_value = False
    return patch.object(CursorManager, "_retriever", new_callable=PropertyMock, return_value=retriever)


def _anchor_line(view: str) -> str:
    """The ``@ ...`` anchor line of a rendered cursor view."""
    return next(line for line in view.splitlines() if line.startswith("@ "))


_CSS = "a {\n  color: red;\n}\n"


class TestOverviewReachesTreeSitterRung:
    """``cursor_overview`` / ``resolve_read_rung`` treat a ``.css`` file as structural, not a dead-end."""

    def test_overview_yields_structural_nodes(self, tmp_path: Path) -> None:
        (tmp_path / "styles.css").write_bytes(_CSS.encode())
        with _force_lsp_miss():
            top = _manager(tmp_path).structural_overview("styles.css")
        assert top  # non-empty: the tree-sitter rung claims .css (was [] before T6)

    def test_read_rung_is_structural_not_plaintext(self, tmp_path: Path) -> None:
        (tmp_path / "styles.css").write_bytes(_CSS.encode())
        with _force_lsp_miss():
            assert _manager(tmp_path).resolve_read_rung("styles.css") is ReadRung.STRUCTURAL

    def test_dockerfile_and_shell_are_structural(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_bytes(b"FROM alpine:3\nRUN echo hi\n")
        (tmp_path / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            assert manager.resolve_read_rung("Dockerfile") is ReadRung.STRUCTURAL
            assert manager.resolve_read_rung("run.sh") is ReadRung.STRUCTURAL
            assert manager.structural_overview("Dockerfile")
            assert manager.structural_overview("run.sh")


class TestCursorStartLandsStructural:
    """``cursor_start`` lands a :class:`StructuralCursorState` for a tree-sitter file, no raise."""

    def test_start_lands_structural_cursor(self, tmp_path: Path) -> None:
        (tmp_path / "styles.css").write_bytes(_CSS.encode())
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            top = manager.structural_overview("styles.css")
            name_path = top[0][0]
            _cid, state = manager.start_cursor(name_path, relative_path="styles.css")
        assert isinstance(state, StructuralCursorState)


class TestZeroBasedLeakPinned:
    """ts-0-leak (pins beb618e8): the structural anchor shows a 1-based cat -n range, never 0-based."""

    def test_anchor_shows_one_based_range_no_zero(self, tmp_path: Path) -> None:
        (tmp_path / "styles.css").write_bytes(_CSS.encode())
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            top = manager.structural_overview("styles.css")
            cid, _ = manager.start_cursor(top[0][0], relative_path="styles.css")
            view = manager.format_cursor_view(cid)
        anchor = _anchor_line(view)
        # the first top-level node starts on file line 1 -> a 1-based range appears on the anchor
        assert re.search(r"styles\.css:1(-\d+)?:", anchor), anchor
        # and no 0-based line ever leaks (the internal row is 0; the converter must have run)
        assert ":0:" not in view
        assert ":0-" not in view


class TestBodyIsExactSourceSlice:
    """structural-symmetry: the addressed node's body renders its exact source bytes."""

    def test_body_roundtrips_node_source(self, tmp_path: Path) -> None:
        (tmp_path / "styles.css").write_bytes(_CSS.encode())
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            top = manager.structural_overview("styles.css")
            cid, state = manager.start_cursor(top[0][0], relative_path="styles.css")
            state.include_body = True
            view = manager.format_cursor_view(cid)
        assert "--- body ---" in view
        # the whole rule -- the top-level node's exact span -- is present verbatim
        assert "a {\n  color: red;\n}" in view


class TestErrorFallthroughNeverRaises:
    """A malformed tree-sitter file degrades to content; no terminal raise (spec-v2 §5.8)."""

    def test_malformed_css_overview_and_rung_do_not_raise(self, tmp_path: Path) -> None:
        (tmp_path / "bad.css").write_bytes(b"a {\n  color: red;")  # unterminated
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            assert manager.resolve_read_rung("bad.css") is ReadRung.STRUCTURAL
            top = manager.structural_overview("bad.css")  # must not raise
        assert isinstance(top, list)  # a (possibly non-empty) listing, never an exception
