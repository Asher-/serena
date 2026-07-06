"""Integration tests for optimistic-concurrency staleness on the cursor surface (spec-v2 §5.5).

The staleness primitive (:mod:`serena.util.staleness`) is unit-tested in
``test/serena/util/test_staleness.py``; this module proves the wiring the T7 task adds on top of
it: the write tools compare-and-swap ``expect_version`` before mutating and refuse a mismatch
(soundness), the whole-file token binds POSITION as well as content (position-bind), a benign
mtime-only touch never strands a valid write (liveness), the version a read projection shows is the
one ``stat`` reports and the gate recomputes (staleness-base), and a cursor whose file is deleted
or renamed away renders a typed gone state instead of stale bytes or a raise (cursor-fate).

The soundness / position-bind / liveness cases drive the live ``cursor_replace_range`` tool through
``python_serena_agent`` (``@pytest.mark.python``); the staleness-base / cursor-fate cases are
hermetic, reusing the MagicMock-project manager harness from ``test_cursor_plaintext``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from serena.cursor import CursorManager
from serena.tools.cursor_tools import CursorReplaceRangeTool
from serena.util.file_lifecycle import FilesystemLifecycle
from serena.util.staleness import read_file_version

if TYPE_CHECKING:
    from serena.agent import SerenaAgent


# --------------------------------------------------------------------------- #
# Hermetic harness — a CursorManager over a MagicMock project rooted at tmp_path
# (mirrors test_cursor_plaintext so the staleness-base / cursor-fate cases need
# no language server).
# --------------------------------------------------------------------------- #


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


def _projected_version(view: str) -> str:
    """Extract the ``version: <token>`` line a cursor projection renders after its anchor."""
    for line in view.splitlines():
        if line.startswith("version:"):
            return line.split("version:", 1)[1].strip()
    raise AssertionError(f"no 'version:' line in projection:\n{view}")


# --------------------------------------------------------------------------- #
# staleness-base + cursor-fate — hermetic (MagicMock project, no LSP)
# --------------------------------------------------------------------------- #


class TestStalenessBase:
    """One byte-based token across surfaces: the read projection, ``stat``, and the gate agree."""

    def test_projection_version_matches_stat_and_read_file_version(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")

        projected = _projected_version(manager.format_cursor_view(cid))

        # the same value the lifecycle stat reports and the raw-bytes helper computes
        assert projected == FilesystemLifecycle(str(tmp_path)).stat("notes.txt").version
        assert projected == read_file_version(str(tmp_path), "notes.txt")


class TestCursorFate:
    """A cursor whose file vanishes renders a typed gone state, never stale bytes or a raise."""

    def test_deleted_file_renders_gone(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")

        (tmp_path / "notes.txt").unlink()  # deleted out from under the open cursor

        view = manager.format_cursor_view(cid)
        assert "no longer exists" in view
        assert "notes.txt" in view

    def test_renamed_away_file_renders_gone(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\n")
        manager = _manager(tmp_path)
        with _force_lsp_miss():
            cid, _ = manager.start_cursor("notes.txt", relative_path="notes.txt")

        (tmp_path / "notes.txt").rename(tmp_path / "renamed.txt")  # renamed away

        assert "no longer exists" in manager.format_cursor_view(cid)


# --------------------------------------------------------------------------- #
# soundness + position-bind + liveness — live cursor_replace_range (needs LSP)
# --------------------------------------------------------------------------- #


@pytest.fixture
def staleness_sandbox(python_serena_agent: "SerenaAgent"):
    """A throwaway python file the staleness edit-path tests mutate; cleaned up after."""
    rel_path = "test_repo/_cursor_staleness_sandbox.py"
    abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / rel_path
    abs_path.write_text("line1\nline2\nline3\n")
    try:
        yield rel_path, abs_path
    finally:
        if abs_path.exists():
            abs_path.unlink()


@pytest.mark.python
class TestStalenessCASOnWriteTools:
    """``cursor_replace_range`` compares-and-swaps ``expect_version`` before mutating."""

    @staticmethod
    def _root(agent: "SerenaAgent") -> str:
        return agent.get_active_project_or_raise().project_root

    def test_soundness_out_of_band_change_refuses_and_leaves_bytes(
        self, python_serena_agent: "SerenaAgent", staleness_sandbox: tuple[str, Path]
    ) -> None:
        rel_path, abs_path = staleness_sandbox
        version = read_file_version(self._root(python_serena_agent), rel_path)

        # the file changes under the caller AFTER the version was taken
        abs_path.write_text("line1\nCHANGED\nline3\n")

        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)
        result = tool.apply(relative_path=rel_path, start_line=1, end_line=1, body="EDIT\n", expect_version=version)

        assert "Stale" in result  # the compare-and-swap refused the write
        assert abs_path.read_text() == "line1\nCHANGED\nline3\n"  # the refused write touched nothing

    def test_position_bind_insert_above_refuses(
        self, python_serena_agent: "SerenaAgent", staleness_sandbox: tuple[str, Path]
    ) -> None:
        rel_path, abs_path = staleness_sandbox
        version = read_file_version(self._root(python_serena_agent), rel_path)

        # a line inserted ABOVE the target leaves the target line's own text unchanged but shifts
        # its position; the whole-file token changes, so the stale view is caught anyway
        abs_path.write_text("inserted\nline1\nline2\nline3\n")

        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)
        result = tool.apply(relative_path=rel_path, start_line=3, end_line=3, body="EDIT\n", expect_version=version)

        assert "Stale" in result

    def test_liveness_mtime_only_touch_still_writes(
        self, python_serena_agent: "SerenaAgent", staleness_sandbox: tuple[str, Path]
    ) -> None:
        rel_path, abs_path = staleness_sandbox
        version = read_file_version(self._root(python_serena_agent), rel_path)

        os.utime(abs_path, (0, 0))  # bump mtime only; the bytes are identical

        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)
        result = tool.apply(relative_path=rel_path, start_line=1, end_line=1, body="EDITED\n", expect_version=version)

        assert "OK" in result  # content-not-mtime: a benign touch never strands a valid write
        assert abs_path.read_text() == "EDITED\nline2\nline3\n"
