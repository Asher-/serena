"""Integration tests for the plaintext floor rung on the cursor surface (spec-v2 §5.1 rung3).

``cursor_overview`` renders a descriptor, ``cursor_start`` lands a whole-file plaintext
cursor whose body round-trips the file's bytes, and nothing dead-ends -- proving the
floor makes any file readable through the cursor surface without a terminal raise.
"""

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from serena.cursor import CursorManager, PlaintextCursorState


def _manager(tmp_path: Path) -> CursorManager:
    """A CursorManager over ``tmp_path`` with a stub project (default 13-backend registry)."""
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.read_file = MagicMock(side_effect=lambda p: Path(tmp_path / p).read_text(encoding="utf-8"))
    return CursorManager(project)


def _force_lsp_miss():
    """Patch the LSP retriever so ``find_unique`` misses -> start_cursor falls to the floor."""
    retriever = MagicMock()
    retriever.find_unique.side_effect = ValueError("no symbol")
    return patch.object(CursorManager, "_retriever", new_callable=PropertyMock, return_value=retriever)


class TestPlaintextOverview:
    """``plaintext_overview`` renders a descriptor instead of a dead-end, and never raises."""

    def test_descriptor_lists_shape_and_encoding(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"one\ntwo\nthree\n")
        out = _manager(tmp_path).plaintext_overview("notes.txt")
        assert "3 lines" in out
        assert "utf-8" in out
        assert "Cannot extract symbols" not in out

    def test_missing_file_is_typed_not_raise(self, tmp_path: Path) -> None:
        out = _manager(tmp_path).plaintext_overview("nope.txt")
        assert "not found" in out


class TestPlaintextCursorStart:
    """``cursor_start`` lands a plaintext cursor for a file no richer rung claims."""

    def test_start_lands_plaintext_cursor_no_raise(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, state = manager.start_cursor("notes.txt", relative_path="notes.txt")
        assert isinstance(state, PlaintextCursorState)
        view = manager.format_cursor_view(cid)
        assert "notes.txt" in view
        assert "2 lines" in view

    def test_body_roundtrips_and_is_1_based(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\ngamma\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")
        view = manager.format_cursor_view(cid)  # include_body defaults True on the floor
        assert "--- body ---" in view
        assert "1: alpha" in view  # 1-based (cat -n), routed through to_display_line
        assert "2: beta" in view
        assert "3: gamma" in view

    def test_absent_file_stays_honest_not_found(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            try:
                manager.start_cursor("nope.txt", relative_path="nope.txt")
                raised = False
            except ValueError:
                raised = True
        assert raised  # a genuinely absent file is honest not-found, never a phantom cursor


class TestPlaintextCursorNeverRaisesOnNonReadOps:
    """Non-read cursor ops tolerate a plaintext cursor: trail renders, symbol edits reject cleanly."""

    def test_format_trail_no_crash(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"x\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")
        out = manager.format_trail(cid)  # would AttributeError without the plaintext branch
        assert "no trail" in out

    def test_reanchor_is_noop(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"x\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, state = manager.start_cursor("notes.txt", relative_path="notes.txt")
        assert manager.reanchor_cursor(cid) is state  # no-op, returns the same state

    def test_symbol_edit_gives_typed_error_not_attributeerror(self) -> None:
        import pytest

        from serena.tools.cursor_tools import CursorReplaceBodyTool

        tool = object.__new__(CursorReplaceBodyTool)
        manager = MagicMock()
        manager.get_cursor.return_value = PlaintextCursorState(cursor_id="c1", relative_path="notes.txt")
        agent = MagicMock()
        agent.get_cursor_manager.return_value = manager
        tool.agent = agent
        with pytest.raises(TypeError, match="plaintext"):
            tool.apply(cursor_id="c1", body="x")
