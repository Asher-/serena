"""
Regression tests for three ``cursor_replace_body`` bugs surfaced by the Markdown
LSP backend:

#. ``CodeEditor.replace_body`` stripped the replacement body unconditionally,
   destroying the trailing newline that the Markdown heading extent contains
   (``end`` at line N+1 col 0). Fix: preserve the leading/trailing whitespace
   envelope of the original extent.

#. ``CursorReplaceBodyTool.apply`` silently returned success even when the
   symbol extent had ballooned and absorbed sibling material (downstream of
   bug #1). Fix: return a diff summary on every edit and raise when the diff
   removes far more lines than the extent could account for.

#. ``LSPFileBuffer`` had no mechanism to resync the language server's document
   copy with disk after an external mutation (e.g. git checkout during project
   re-activation). Fix: ``LSPFileBuffer.reload_from_disk`` re-reads from disk
   and, when the buffer is open in the language server, pushes a full-document
   ``didChange`` notification.

The tests here exercise the helpers at unit level to keep the test suite fast
and independent of per-language LSP availability. A companion test in
``test/solidlsp/test_ls_common.py`` covers bug #3 end-to-end against a running
language server.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from serena.code_editor import CodeEditor
from serena.symbol import PositionInFile
from serena.tools import SUCCESS_RESULT
from serena.tools.cursor_tools import CursorReplaceBodyTool


class _InMemoryEditedFile(CodeEditor.EditedFile):
    """
    Minimal ``CodeEditor.EditedFile`` implementation that stores contents
    in-memory, used to exercise ``text_between_positions`` and the
    envelope-preserving replacement logic without a running language server.
    """

    def __init__(self, relative_path: str, contents: str) -> None:
        super().__init__(relative_path)
        self._contents = contents

    def get_contents(self) -> str:
        return self._contents

    def set_contents(self, contents: str) -> None:
        self._contents = contents

    def delete_text_between_positions(self, start_pos: PositionInFile, end_pos: PositionInFile) -> None:
        # reuse the helper under test for position→index mapping so delete
        # stays consistent with the surrounding logic
        start_idx = self._index(start_pos)
        end_idx = self._index(end_pos)
        self._contents = self._contents[:start_idx] + self._contents[end_idx:]

    def insert_text_at_position(self, pos: PositionInFile, text: str) -> None:
        idx = self._index(pos)
        self._contents = self._contents[:idx] + text + self._contents[idx:]

    def _index(self, pos: PositionInFile) -> int:
        # line/col → char offset, mirroring TextUtils.get_index_from_line_col so
        # the stub stays honest to what the real LSP-backed implementation does
        lines = self._contents.splitlines(keepends=True)
        offset = sum(len(line) for line in lines[: pos.line])
        offset += pos.col
        return offset


class TestTextBetweenPositions:
    """Exercises the ``text_between_positions`` helper added to ``EditedFile``."""

    def test_same_line_slice(self) -> None:
        """Within a single line, slices by column range (start inclusive, end exclusive)."""
        ef = _InMemoryEditedFile("x.md", "hello world\n")
        start = PositionInFile(line=0, col=0)
        end = PositionInFile(line=0, col=5)
        assert ef.text_between_positions(start, end) == "hello"

    def test_multiline_slice_includes_newline(self) -> None:
        """Across lines, the returned text carries the intervening newlines."""
        ef = _InMemoryEditedFile("x.md", "one\ntwo\nthree\n")
        start = PositionInFile(line=0, col=0)
        end = PositionInFile(line=2, col=0)
        assert ef.text_between_positions(start, end) == "one\ntwo\n"

    def test_extent_ending_at_next_line_col_zero_captures_trailing_newline(self) -> None:
        """
        The markdown-style heading extent pattern: a heading on line N with
        ``end`` at line N+1 col 0. The text between start and end is the
        heading plus its trailing newline — that newline is what the old
        ``body.strip()`` destroyed.
        """
        ef = _InMemoryEditedFile("x.md", "## Foo\n## Bar\n")
        start = PositionInFile(line=0, col=0)
        end = PositionInFile(line=1, col=0)
        assert ef.text_between_positions(start, end) == "## Foo\n"


class TestReplaceBodyEnvelope:
    """
    Exercises the leading/trailing whitespace envelope logic by driving the
    delete + insert pair the way ``CodeEditor.replace_body`` does. The fix
    under test captures ``text_between_positions`` first, extracts the
    envelope, and re-applies it around the user's stripped body.
    """

    @staticmethod
    def _apply_envelope_replace(ef: _InMemoryEditedFile, start: PositionInFile, end: PositionInFile, body: str) -> None:
        # this mirrors the post-fix replace_body logic end-to-end
        original = ef.text_between_positions(start, end)
        leading = original[: len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()) :]
        framed = leading + body.strip() + trailing
        ef.delete_text_between_positions(start, end)
        ef.insert_text_at_position(start, framed)

    def test_markdown_heading_replacement_preserves_next_heading_line(self) -> None:
        """
        The regression from the user report: replacing a markdown heading's
        extent (which ends at next line col 0) used to smush the following
        heading onto the same line. The envelope preservation keeps the
        trailing newline intact.
        """
        ef = _InMemoryEditedFile("x.md", "## Foo\n## Bar\n")
        start = PositionInFile(line=0, col=0)
        end = PositionInFile(line=1, col=0)

        self._apply_envelope_replace(ef, start, end, "## Foo Renamed")

        assert ef.get_contents() == "## Foo Renamed\n## Bar\n"
        # crucially, the second heading still starts on its own line
        assert "## Foo Renamed\n## Bar" in ef.get_contents()

    def test_tight_extent_envelope_is_empty_so_strip_dominates(self) -> None:
        """
        For languages with tight extents (Python/Swift/C++), leading and
        trailing whitespace around the extent are empty strings, so the
        envelope preservation is a no-op and the historical ``body.strip()``
        behaviour is preserved.
        """
        ef = _InMemoryEditedFile("x.py", "def foo():\n    return 1")
        start = PositionInFile(line=0, col=0)
        end = PositionInFile(line=1, col=len("    return 1"))

        self._apply_envelope_replace(ef, start, end, "  def foo():\n    return 2  \n")

        # leading/trailing whitespace supplied by the caller is stripped because
        # the original extent itself has no surrounding whitespace envelope
        assert ef.get_contents() == "def foo():\n    return 2"


class TestCountDiffLines:
    """
    Exercises ``CursorReplaceBodyTool._count_diff_lines`` — the helper that
    powers the bug #2 diff summary and the gross-over-deletion safety net.
    """

    def test_identical_texts_yield_zero_zero(self) -> None:
        """Identical before/after produce no added or removed lines."""
        removed, added = CursorReplaceBodyTool._count_diff_lines("a\nb\n", "a\nb\n")
        assert (removed, added) == (0, 0)

    def test_pure_addition_counts_only_additions(self) -> None:
        """Appending lines produces only additions in the unified diff."""
        removed, added = CursorReplaceBodyTool._count_diff_lines("a\n", "a\nb\nc\n")
        assert removed == 0
        assert added == 2

    def test_pure_removal_counts_only_removals(self) -> None:
        """Deleting lines produces only removals in the unified diff."""
        removed, added = CursorReplaceBodyTool._count_diff_lines("a\nb\nc\n", "a\n")
        assert removed == 2
        assert added == 0

    def test_large_deletion_reports_high_removed_count(self) -> None:
        """
        The bug #2 safety net depends on this: a balloon-induced silent
        deletion of many lines must surface as a large ``removed`` count
        that the caller can compare against the symbol's extent.
        """
        before = "".join(f"line {i}\n" for i in range(40))
        after = "line 0\n"
        removed, added = CursorReplaceBodyTool._count_diff_lines(before, after)
        assert removed >= 39
        assert added == 0


class TestStripRedundantLeadingPrefix:
    """
    Exercises ``CodeEditor._strip_redundant_leading_prefix`` — the guard that
    prevents the doubled ``var`` / ``type`` keyword corruption when a language
    server reports a declaration extent that begins at the symbol *name* rather
    than the leading keyword (the Go/gopls case in
    ``bug://serena/cursor-replace-body-nonatomic-doubles-decl-keyword``).
    """

    def test_go_var_keyword_is_elided(self) -> None:
        """A body repeating the ``var`` keyword already on the line is de-duplicated."""
        assert CodeEditor._strip_redundant_leading_prefix("var ", "var X = expr") == "X = expr"

    def test_go_type_keyword_is_elided(self) -> None:
        """The same applies to ``type`` declarations (``type type Foo`` corruption)."""
        assert CodeEditor._strip_redundant_leading_prefix("type ", "type Foo struct {}") == "Foo struct {}"

    def test_indented_prefix_keyword_is_elided_but_body_remainder_kept(self) -> None:
        """Indentation stays in the file (it is part of the prefix); only the keyword is dropped."""
        assert CodeEditor._strip_redundant_leading_prefix("    var ", "var x = 2") == "x = 2"

    def test_body_without_keyword_passes_through(self) -> None:
        """A body that correctly omits the keyword must be left untouched (no over-strip)."""
        assert CodeEditor._strip_redundant_leading_prefix("var ", "X = expr") == "X = expr"

    def test_empty_prefix_is_noop(self) -> None:
        """Functions / markdown / Python-widened extents have no keyword prefix."""
        assert CodeEditor._strip_redundant_leading_prefix("", "func foo() {}") == "func foo() {}"

    def test_whitespace_only_prefix_is_noop(self) -> None:
        """Indentation alone (extent already includes the keyword) strips nothing."""
        assert CodeEditor._strip_redundant_leading_prefix("    ", "value = 1") == "value = 1"

    def test_shared_spelling_without_word_boundary_is_not_elided(self) -> None:
        """``various`` merely starts with ``var``; without a following space we must not bite in."""
        assert CodeEditor._strip_redundant_leading_prefix("var ", "various = 1") == "various = 1"


class TestReanchorAndFormat:
    """
    Exercises ``CursorReplaceBodyTool._reanchor_and_format`` — the atomicity guard
    that keeps an already-applied, successful edit from surfacing as a bare error
    when the post-edit cursor re-anchor fails (the silent-corruption half of
    ``bug://serena/cursor-replace-body-nonatomic-doubles-decl-keyword``: file
    mutated, but the caller saw only ``No symbol matching ...``).
    """

    def test_successful_reanchor_returns_cursor_view(self) -> None:
        """On a clean re-anchor the result carries the success marker, diff, and view."""
        manager = MagicMock()
        manager.format_cursor_view.return_value = "@ Foo :Function@x.go:1-3:"
        result = CursorReplaceBodyTool._reanchor_and_format(manager, "c1", "Diff: -1 / +1 lines")
        manager.reanchor_cursor.assert_called_once_with("c1")
        assert result.startswith(SUCCESS_RESULT)
        assert "Diff: -1 / +1 lines" in result
        assert "@ Foo :Function@x.go:1-3:" in result

    def test_reanchor_failure_still_reports_success(self) -> None:
        """A ValueError from re-anchor must NOT mask the already-applied edit."""
        manager = MagicMock()
        manager.reanchor_cursor.side_effect = ValueError("No symbol matching 'Foo' found")
        result = CursorReplaceBodyTool._reanchor_and_format(manager, "c1", "Diff: -1 / +1 lines")
        assert result.startswith(SUCCESS_RESULT)
        assert "Diff: -1 / +1 lines" in result
        assert "could not re-anchor" in result
        assert "No symbol matching 'Foo' found" in result
        # the view is not rendered when re-anchoring fails
        manager.format_cursor_view.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
