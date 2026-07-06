"""
Certification tests for the 1-based line-number contract (spec-v2 §5.7).

The cursor surface must display 1-based line numbers everywhere (``cat -n``
equivalent), accept 1-based line arguments on the write tools, and round-trip
read->write so a number an agent reads back is the number it can write to.
These pin the reconciled ``beb618e8`` ``--- body ---`` leak and
``cursor_replace_range_verified``'s previously-0-based args.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from serena.tools.cursor_tools import (
    CursorConfigureTool,
    CursorOverviewTool,
    CursorReplaceRangeTool,
    CursorReplaceRangeVerifiedTool,
    CursorStartTool,
)

if TYPE_CHECKING:
    from serena.agent import SerenaAgent

pytestmark = pytest.mark.python


class TestReadDisplaysOneBased:
    """Read tools display 1-based line numbers that agree with ``cat -n``."""

    def test_overview_reports_cat_n_line_for_top_level_symbol(self, python_serena_agent: "SerenaAgent") -> None:
        """``UserService`` sits on ``cat -n`` line 10 of services.py (0-based LSP
        line 9); the overview must display the ``cat -n`` number, not the 0-based one.
        """
        overview_tool = python_serena_agent.get_tool(CursorOverviewTool)
        result = overview_tool.apply(
            because="pin cat-n equivalence for the read surface",
            relative_path=os.path.join("test_repo", "services.py"),
        )
        entries = [ln for ln in result.splitlines() if "UserService :" in ln]
        assert len(entries) == 1, f"expected one UserService entry, got {entries!r}"
        # entry shape: ``  UserService :Class@<path>:<line>:``
        line_field = entries[0].rsplit(":", 2)[-2]
        assert line_field == "10", f"expected 1-based cat -n line 10, got entry {entries[0]!r}"

    def test_no_zero_based_line_leaks_in_overview(self, python_serena_agent: "SerenaAgent") -> None:
        """No overview entry may cite ``:0:`` — there is no line 0 under ``cat -n``,
        so a ``:0:`` is always a 0-based leak.
        """
        overview_tool = python_serena_agent.get_tool(CursorOverviewTool)
        result = overview_tool.apply(
            because="guard against 0-based leaks in the read surface",
            relative_path=os.path.join("test_repo", "services.py"),
        )
        anchor_entries = [ln for ln in result.splitlines() if " :" in ln and "@" in ln]
        assert anchor_entries, "expected at least one symbol entry in the overview"
        for entry in anchor_entries:
            assert not entry.rstrip().endswith(":0:"), f"0-based line leaked into overview entry: {entry!r}"


@pytest.fixture
def four_line_file(python_serena_agent: "SerenaAgent") -> Iterator[tuple[str, Path]]:
    """A four-line file ``A/B/C/D`` for read<->write symmetry assertions."""
    rel_path = os.path.join("test_repo", "_cursor_line_base_sandbox.py")
    abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / rel_path
    abs_path.write_text("A = 1\nB = 2\nC = 3\nD = 4\n")
    try:
        python_serena_agent.reset_language_server_manager()
    except Exception:
        pass
    try:
        yield rel_path, abs_path
    finally:
        if abs_path.exists():
            abs_path.unlink()
        try:
            python_serena_agent.reset_language_server_manager()
        except Exception:
            pass


class TestWriteAcceptsOneBased:
    """Write tools accept 1-based line arguments (read<->write symmetry)."""

    def test_replace_range_edits_the_one_based_line(self, python_serena_agent: "SerenaAgent", four_line_file: tuple[str, Path]) -> None:
        """1-based line 2 is ``B = 2``; replacing [2, 2] must edit that line and
        leave ``C = 3`` (which was 0-based index 2) untouched.
        """
        rel_path, abs_path = four_line_file
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)
        tool.apply(relative_path=rel_path, start_line=2, end_line=2, body="B = 22\n", expect_version="*")
        assert abs_path.read_text() == "A = 1\nB = 22\nC = 3\nD = 4\n"

    def test_replace_range_verified_matches_one_based_expected(
        self, python_serena_agent: "SerenaAgent", four_line_file: tuple[str, Path]
    ) -> None:
        """The verified variant reads and writes the same 1-based line: the content
        expected at line 3 is ``C = 3``.
        """
        rel_path, abs_path = four_line_file
        tool = python_serena_agent.get_tool(CursorReplaceRangeVerifiedTool)
        tool.apply(relative_path=rel_path, start_line=3, end_line=3, expected_content="C = 3\n", body="C = 33\n", expect_version="*")
        assert abs_path.read_text() == "A = 1\nB = 2\nC = 33\nD = 4\n"

    def test_replace_range_rejects_line_zero(self, python_serena_agent: "SerenaAgent", four_line_file: tuple[str, Path]) -> None:
        """Line 0 is not a valid 1-based line; the tool rejects it before any I/O."""
        rel_path, _ = four_line_file
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)
        with pytest.raises(ValueError, match="invalid range"):
            tool.apply(relative_path=rel_path, start_line=0, end_line=0, body="x\n", expect_version="*")

    def test_verified_drift_message_cites_one_based_range(
        self, python_serena_agent: "SerenaAgent", four_line_file: tuple[str, Path]
    ) -> None:
        """A drift error must cite the range 1-based (``:2-2``), never 0-based."""
        rel_path, _ = four_line_file
        tool = python_serena_agent.get_tool(CursorReplaceRangeVerifiedTool)
        with pytest.raises(ValueError) as excinfo:
            tool.apply(relative_path=rel_path, start_line=2, end_line=2, expected_content="not B\n", body="x\n", expect_version="*")
        msg = str(excinfo.value)
        assert "drift detected" in msg
        assert f"{rel_path}:2-2" in msg


@pytest.fixture
def bodied_file(python_serena_agent: "SerenaAgent") -> Iterator[tuple[str, Path]]:
    """A file whose function ``sample_fn`` starts on ``cat -n`` line 3 (two leading
    blank lines) so a 0-based body-start (2) is visibly distinct from the 1-based 3.
    """
    rel_path = os.path.join("test_repo", "_cursor_line_base_body_sandbox.py")
    abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / rel_path
    abs_path.write_text("\n\ndef sample_fn():\n    return 7\n")
    try:
        python_serena_agent.reset_language_server_manager()
    except Exception:
        pass
    try:
        yield rel_path, abs_path
    finally:
        if abs_path.exists():
            abs_path.unlink()
        try:
            python_serena_agent.reset_language_server_manager()
        except Exception:
            pass


class TestBodyNumberingIsOneBased:
    """The ``--- body ---`` projection numbers from the 1-based file line (beb618e8)."""

    def test_body_first_line_is_cat_n_number(self, python_serena_agent: "SerenaAgent", bodied_file: tuple[str, Path]) -> None:
        """``def sample_fn():`` is on ``cat -n`` line 3; the body block must number
        it 3, not the 0-based 2 that the beb618e8 leak emitted.
        """
        rel_path, _ = bodied_file
        start_tool = python_serena_agent.get_tool(CursorStartTool)
        configure_tool = python_serena_agent.get_tool(CursorConfigureTool)
        start_tool.apply(
            because="pin the 1-based body-line numbering (beb618e8)",
            name_path="sample_fn",
            relative_path=rel_path,
            cursor_id="lb-body",
        )
        view = configure_tool.apply(cursor_id="lb-body", include_body=True)
        assert "--- body ---" in view
        assert "3: def sample_fn():" in view
        assert "2: def sample_fn():" not in view
