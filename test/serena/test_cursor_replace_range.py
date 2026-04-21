"""
Tests for ``CodeEditor.replace_lines`` and ``CursorReplaceRangeTool`` — the
file-line-range editing primitive that covers the regions the symbolic
cursor tools cannot reach (free-floating comment blocks, blank-line gaps
between imports, imports themselves on LSPs that do not expose them as
symbols, license headers, and any content before the first declaration).

The unit tests in ``TestReplaceLinesUnit`` drive the primitive through an
in-memory ``CodeEditor`` subclass so they exercise the algorithm without
starting a language server; this is also how the language-independence
claim is verified (the primitive never consults the LSP, so the same test
passes whether the fixture looks like Swift, TypeScript, or Python).

The integration tests in ``TestCursorReplaceRangeTool`` drive the MCP tool
end-to-end through a live ``python_serena_agent`` so the full stack
(``project.read_file`` → ``CodeEditor.replace_lines`` →
``_save_edited_file``) is covered.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from serena.code_editor import CodeEditor
from serena.symbol import PositionInFile
from serena.tools.cursor_tools import CursorReplaceRangeTool

if TYPE_CHECKING:
    from serena.agent import SerenaAgent


# --------------------------------------------------------------------------- #
# In-memory harness — mirrors the one used in ``test_cursor_replace_body_bugs``
# --------------------------------------------------------------------------- #


class _InMemoryEditedFile(CodeEditor.EditedFile):
    """
    Minimal ``CodeEditor.EditedFile`` implementation that stores contents
    in-memory; enables LSP-free testing of ``CodeEditor.replace_lines``.
    """

    def __init__(self, relative_path: str, contents: str) -> None:
        super().__init__(relative_path)
        self._contents = contents

    def get_contents(self) -> str:
        return self._contents

    def set_contents(self, contents: str) -> None:
        self._contents = contents

    def delete_text_between_positions(self, start_pos: PositionInFile, end_pos: PositionInFile) -> None:
        start_idx = self._index(start_pos)
        end_idx = self._index(end_pos)
        self._contents = self._contents[:start_idx] + self._contents[end_idx:]

    def insert_text_at_position(self, pos: PositionInFile, text: str) -> None:
        idx = self._index(pos)
        self._contents = self._contents[:idx] + text + self._contents[idx:]

    def _index(self, pos: PositionInFile) -> int:
        lines = self._contents.splitlines(keepends=True)
        offset = sum(len(line) for line in lines[: pos.line])
        offset += pos.col
        return offset


class _InMemoryCodeEditor(CodeEditor):
    """
    Concrete ``CodeEditor`` whose ``_open_file_context`` yields an
    in-memory edited file. Used to exercise ``replace_lines`` without
    spinning up a language server.
    """

    def __init__(self, files: dict[str, str]) -> None:
        # bypass CodeEditor.__init__ (which needs a full Project) and set only
        # the attributes replace_lines depends on; the in-memory file skips
        # the on-disk save path so project_root never gets dereferenced
        self.project_root = "."
        self.encoding = "utf-8"
        self.newline = "\n"
        self._files = files

    @contextmanager
    def _open_file_context(self, relative_path: str) -> Iterator["CodeEditor.EditedFile"]:
        edited = _InMemoryEditedFile(relative_path, self._files[relative_path])
        yield edited

    def _save_edited_file(self, edited_file: "CodeEditor.EditedFile") -> None:
        # persist back into the in-memory dict so assertions can read the result
        self._files[edited_file.relative_path] = edited_file.get_contents()

    def _find_unique_symbol(self, name_path, relative_file_path):  # type: ignore[override]
        # symbol lookup is not exercised by replace_lines; stub to keep the
        # abstract base class happy for in-memory tests
        raise NotImplementedError

    def rename_symbol(self, name_path, relative_file_path, new_name):  # type: ignore[override]
        # same rationale as _find_unique_symbol
        raise NotImplementedError

    def get(self, relative_path: str) -> str:
        return self._files[relative_path]


# --------------------------------------------------------------------------- #
# Unit tests — language-independence is demonstrated by using file contents
# that could be Swift / TypeScript / Python; the primitive does not care.
# --------------------------------------------------------------------------- #


class TestReplaceLinesUnit:
    """
    Exercises ``CodeEditor.replace_lines`` in isolation, covering the three
    concrete failing cases from the upstream handoff plus boundary behaviour
    (empty-body deletion, single-line replacement, trailing-newline handling,
    and input-validation).
    """

    def test_deletes_top_of_file_comment_block_in_swift_fixture(self) -> None:
        """
        Case (a): a ``///`` doc block sits atop a Swift file above the first
        declaration. ``cursor_*`` symbolic tools cannot reach it; the
        ``replace_lines`` primitive deletes it cleanly.
        """
        # fixture: 6-line /// block followed by a blank line and the first decl
        swift = (
            "/// Orphan header describing the file.\n"
            "///\n"
            "/// Inserted by a prior edit that has since moved the referenced\n"
            "/// symbol elsewhere, so the block is now detached.\n"
            "///\n"
            "/// Will be removed.\n"
            "\n"
            "@MainActor struct TagManagerTests {}\n"
        )
        editor = _InMemoryCodeEditor({"TagManagerTests.swift": swift})

        # delete lines 0..5 inclusive (the /// block); keep the blank line
        # separating the block from the declaration so the replacement has no
        # structural side effect on the surviving content
        editor.replace_lines("TagManagerTests.swift", start_line=0, end_line=5, content="")

        assert editor.get("TagManagerTests.swift") == (
            "\n" "@MainActor struct TagManagerTests {}\n"
        )

    def test_reorders_imports_in_typescript_fixture(self) -> None:
        """
        Case (b): two unordered ``import`` statements separated by a blank-line
        gap. Neither statement is a reachable LSP symbol in some TS servers;
        ``replace_lines`` reorders them by rewriting the whole import block.
        """
        ts = (
            'import { Z } from "./z";\n'
            "\n"
            'import { A } from "./a";\n'
            'import { M } from "./m";\n'
            "\n"
            "export function entry() {}\n"
        )
        editor = _InMemoryCodeEditor({"entry.ts": ts})

        # replace lines 0..4 (the three imports and the stray blank line
        # gap between them) with an alphabetised, gap-free import block
        reordered = 'import { A } from "./a";\n' 'import { M } from "./m";\n' 'import { Z } from "./z";\n'
        editor.replace_lines("entry.ts", start_line=0, end_line=4, content=reordered)

        assert editor.get("entry.ts") == (
            'import { A } from "./a";\n'
            'import { M } from "./m";\n'
            'import { Z } from "./z";\n'
            "export function entry() {}\n"
        )

    def test_updates_single_doc_comment_line_above_class(self) -> None:
        """
        Case (c): a ``///`` doc line that sits above a class. In Python we use
        a ``#`` free-floating comment for the same shape; the primitive treats
        both identically because it operates on bytes, not on syntax.
        """
        py = (
            "# pg_hba auth is 127.0.0.1/32 trust\n"
            "# (stale — needs update)\n"
            "class BrainPostgresClient:\n"
            "    pass\n"
        )
        editor = _InMemoryCodeEditor({"brain.py": py})

        # replace only line 0 (single-line range: start_line == end_line)
        editor.replace_lines(
            "brain.py",
            start_line=0,
            end_line=0,
            content="# pg_hba auth: 127.0.0.1/32 trust + LAN trust rules\n",
        )

        assert editor.get("brain.py") == (
            "# pg_hba auth: 127.0.0.1/32 trust + LAN trust rules\n"
            "# (stale — needs update)\n"
            "class BrainPostgresClient:\n"
            "    pass\n"
        )

    def test_empty_body_deletes_range_entirely(self) -> None:
        """
        Passing ``content=""`` is the canonical "delete this line range"
        shorthand — verifies that the insert step is skipped when the body
        is empty and that no trailing empty string is left behind.
        """
        text = "keep-0\ndrop-1\ndrop-2\nkeep-3\n"
        editor = _InMemoryCodeEditor({"x": text})

        editor.replace_lines("x", start_line=1, end_line=2, content="")

        assert editor.get("x") == "keep-0\nkeep-3\n"

    def test_body_without_trailing_newline_joins_following_line(self) -> None:
        """
        The primitive inserts ``body`` verbatim; if the caller omits a
        trailing newline the next line will be concatenated. This test locks
        the documented behaviour in so a future refactor does not silently
        introduce auto-newline injection.
        """
        text = "a\nb\nc\n"
        editor = _InMemoryCodeEditor({"x": text})

        editor.replace_lines("x", start_line=0, end_line=0, content="A-no-newline")

        assert editor.get("x") == "A-no-newlineb\nc\n"

    def test_crlf_line_endings_are_preserved_across_the_edit(self) -> None:
        """
        The primitive operates on character positions; the in-memory file
        preserves whatever line terminator was present. CRLF files stay CRLF
        outside the edited range; the caller is responsible for providing
        the desired terminator in ``body``.
        """
        crlf = "alpha\r\nbeta\r\ngamma\r\n"
        editor = _InMemoryCodeEditor({"x.txt": crlf})

        editor.replace_lines("x.txt", start_line=1, end_line=1, content="BETA\r\n")

        assert editor.get("x.txt") == "alpha\r\nBETA\r\ngamma\r\n"

    def test_utf8_multibyte_content_is_preserved(self) -> None:
        """
        Non-ASCII characters outside the edited range are left untouched;
        content inside is replaced verbatim without re-encoding. Covers the
        ``line bounds are UTF-8 aware`` requirement from the handoff.
        """
        text = "α\nβ中文\nγ\n"
        editor = _InMemoryCodeEditor({"greek.txt": text})

        editor.replace_lines("greek.txt", start_line=1, end_line=1, content="β-替换\n")

        assert editor.get("greek.txt") == "α\nβ-替换\nγ\n"

    def test_invalid_range_raises_value_error(self) -> None:
        """
        ``start_line < 0`` or ``end_line < start_line`` is a programmer
        error; the primitive fails fast with ``ValueError`` rather than
        producing a silently broken file.
        """
        editor = _InMemoryCodeEditor({"x": "a\nb\n"})

        with pytest.raises(ValueError):
            editor.replace_lines("x", start_line=-1, end_line=0, content="")

        with pytest.raises(ValueError):
            editor.replace_lines("x", start_line=3, end_line=2, content="")


# --------------------------------------------------------------------------- #
# Integration tests — drive the MCP tool end-to-end through the live agent.
# --------------------------------------------------------------------------- #


pytestmark = pytest.mark.python

# reuse the pre-existing python_serena_agent fixture from test_cursor_navigation
from test.serena.test_cursor_navigation import python_serena_agent  # noqa: E402, F401


@pytest.fixture
def throwaway_layout_file(python_serena_agent: "SerenaAgent") -> Iterator[str]:
    """
    Creates a Python file whose top-of-file region mimics the three concrete
    failing cases from the upstream handoff (detached comment block, unordered
    imports with a blank gap, stale doc comment above a class), so a single
    file can drive all three integration assertions.
    """
    rel_path = "test_repo/_cursor_replace_range_sandbox.py"
    abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / rel_path
    abs_path.write_text(
        "# Orphan header describing the file.\n"
        "# Inserted by a prior edit, now detached.\n"
        "# Will be removed.\n"
        "\n"
        "import z\n"
        "\n"
        "import a\n"
        "import m\n"
        "\n"
        "# pg_hba auth is 127.0.0.1/32 trust (stale)\n"
        "class BrainPostgresClient:\n"
        "    pass\n"
    )
    try:
        python_serena_agent.reset_language_server_manager()
    except Exception:
        pass
    try:
        yield rel_path
    finally:
        if abs_path.exists():
            abs_path.unlink()
        try:
            python_serena_agent.reset_language_server_manager()
        except Exception:
            pass


class TestCursorReplaceRangeTool:
    """
    End-to-end tests that drive ``CursorReplaceRangeTool`` via
    ``python_serena_agent.get_tool(...)``; the same code path the MCP server
    uses when responding to a ``cursor_replace_range`` call.
    """

    def test_deletes_detached_top_of_file_comment_block(
        self, python_serena_agent: "SerenaAgent", throwaway_layout_file: str
    ) -> None:
        """Case (a): top-of-file ``#`` comment block is removed; the rest of
        the file (imports, class) is untouched."""
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)

        result = tool.apply(relative_path=throwaway_layout_file, start_line=0, end_line=2, body="")

        assert "OK" in result
        assert "Diff:" in result

        abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / throwaway_layout_file
        content = abs_path.read_text()
        assert "Orphan header" not in content
        # the blank line that used to follow the header now sits at the top
        assert content.startswith("\nimport z\n")
        # the class survived intact
        assert "class BrainPostgresClient:" in content

    def test_reorders_imports_across_blank_line_gap(
        self, python_serena_agent: "SerenaAgent", throwaway_layout_file: str
    ) -> None:
        """Case (b): three imports with a stray blank line between them are
        collapsed into a single alphabetised import block."""
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)

        # first, remove the header (lines 0..2) via the tool, then reorder the
        # imports; this also exercises two sequential edits on the same file
        tool.apply(relative_path=throwaway_layout_file, start_line=0, end_line=2, body="")
        # after the first edit, the file starts with a blank line then imports.
        # replace lines 1..5 (import z, blank, import a, import m, blank) with
        # the alphabetised block.
        result = tool.apply(
            relative_path=throwaway_layout_file,
            start_line=1,
            end_line=5,
            body="import a\nimport m\nimport z\n\n",
        )

        assert "OK" in result

        abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / throwaway_layout_file
        content = abs_path.read_text()
        assert "import a\nimport m\nimport z\n" in content
        # the class survived intact
        assert "class BrainPostgresClient:" in content

    def test_updates_single_doc_comment_line_above_class(
        self, python_serena_agent: "SerenaAgent", throwaway_layout_file: str
    ) -> None:
        """Case (c): rewrite a single ``#`` doc-comment line that sits above a
        class. ``cursor_replace_body`` cannot reach it (it is not a symbol)."""
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)

        # the doc comment is at line 9 in the fixture (0-based)
        result = tool.apply(
            relative_path=throwaway_layout_file,
            start_line=9,
            end_line=9,
            body="# pg_hba auth: 127.0.0.1/32 trust + LAN trust rules\n",
        )

        assert "OK" in result

        abs_path = Path(python_serena_agent.get_active_project_or_raise().project_root) / throwaway_layout_file
        content = abs_path.read_text()
        assert "# pg_hba auth: 127.0.0.1/32 trust + LAN trust rules" in content
        assert "stale" not in content
        # the following class must remain on its own line
        assert "\nclass BrainPostgresClient:\n" in content

    def test_invalid_range_surfaces_as_value_error(self, python_serena_agent: "SerenaAgent") -> None:
        """Invalid ranges fail fast before any file I/O."""
        tool = python_serena_agent.get_tool(CursorReplaceRangeTool)

        with pytest.raises(ValueError):
            tool.apply(relative_path="test_repo/does_not_matter.py", start_line=5, end_line=2, body="")
