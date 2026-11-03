"""unit tests for :class:`serena.tools.file_tools.CreateTextFileTool`.

the tool is path-addressed rather than symbol-anchored, so it is the one edit tool that can
bootstrap a brand-new file and write empty / zero-byte content -- neither of which a cursor or
symbol edit can do, since an empty file has no symbol to anchor to. these tests pin that
contract: new-file creation, zero-byte writes, overwrite reporting, parent-directory creation,
resolution against the active project root, and verbatim UTF-8 byte preservation (the handle is
opened with ``newline=""`` so no newline translation occurs).

the tool is driven through ``apply()`` directly with a minimal duck-typed agent, mirroring
``test_config_tools._DummyAgent`` -- ``apply()`` only reads the project root, so the
task-executor / tool-registry / project-state machinery is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.config.serena_config import SerenaConfig
from serena.constants import DEFAULT_SOURCE_FILE_ENCODING
from serena.project import Project
from serena.tools import ReadFileTool
from serena.tools.file_tools import (
    CreateTextFileTool,
    DeleteFileTool,
    FindFileTool,
    ListDirTool,
    RenameFileTool,
    StatTool,
)
from solidlsp.ls_utils import TextUtils


@dataclass
class _ProjectStub:
    """minimal stand-in for :class:`Project`.

    exposes ``project_root`` (the sole attribute :meth:`Tool.get_project_root` reads off the active
    project) and ``is_ignored_path`` (the ignore predicate the lifecycle tools thread into the service).
    """

    project_root: str

    def is_ignored_path(self, path: str) -> bool:
        # the stub project ignores nothing; gitignore policy is exercised at the service level
        return False

    def validate_relative_path(self, relative_path: str) -> None:
        # the stub project confines nothing; confinement is exercised at the service level
        pass


class _DummyAgent:
    """the smallest agent surface :class:`CreateTextFileTool` needs when invoked via ``apply()``.

    ``Tool.project`` reads ``agent.get_active_project_or_raise()`` and the tool then takes only
    ``.project_root`` from the returned project, so a stub project over a temp directory is
    sufficient; no task-executor or project-state machinery is involved.
    """

    def __init__(self, project_root: str) -> None:
        self._project = _ProjectStub(project_root=project_root)

    def get_active_project_or_raise(self) -> _ProjectStub:
        return self._project


def _make_tool(project_root: Path) -> CreateTextFileTool:
    # Tool's base constructor only stores ``agent``; the temp project root flows in via the stub
    # agent so apply() resolves relative paths against ``project_root``.
    return CreateTextFileTool(agent=_DummyAgent(str(project_root)))


def _make(tool_cls, project_root: Path):
    # generic builder over the same stub agent, for the lifecycle tools beyond create
    return tool_cls(agent=_DummyAgent(str(project_root)))


def test_create_new_file_writes_content_and_reports_created(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    # write a brand-new top-level file
    result = tool.apply("notes.txt", "hello world")

    # the file holds exactly the given content, and the report says "Created" with the char count
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello world"
    assert result == "Created notes.txt (11 characters)."


def test_empty_content_yields_zero_byte_file(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    # the default empty content must produce a genuine zero-byte file, not a one-newline file
    result = tool.apply("empty.txt")

    target = tmp_path / "empty.txt"
    assert target.is_file()
    assert target.stat().st_size == 0
    assert result == "Created empty.txt (0 characters)."


def test_overwrite_existing_file_replaces_content_and_reports_overwrote(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    # seed a file, then overwrite it through the tool -- overwriting now requires explicit intent
    tool.apply("data.txt", "original contents")
    result = tool.apply("data.txt", "replaced!", overwrite=True)

    # the second write reports "Overwrote" and the content is fully replaced (not appended)
    assert (tmp_path / "data.txt").read_text(encoding="utf-8") == "replaced!"
    assert result == "Overwrote data.txt (9 characters)."


def test_parent_directories_are_created(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    # a nested relative path whose parent directories do not yet exist
    result = tool.apply("a/b/c/deep.txt", "x")

    # every intermediate directory is created and the file lands at the nested path
    target = tmp_path / "a" / "b" / "c" / "deep.txt"
    assert target.read_text(encoding="utf-8") == "x"
    assert result == "Created a/b/c/deep.txt (1 characters)."


def test_relative_path_is_resolved_against_project_root(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    # the path is interpreted relative to the stub project root, not the process cwd
    tool.apply("sub/file.txt", "anchored")

    assert (tmp_path / "sub" / "file.txt").is_file()


def test_content_is_written_verbatim_without_newline_translation(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    # mixed CRLF / LF content must survive byte-for-byte (handle opened with newline="");
    # cr / lf are built with chr() to keep the expected bytes explicit and unambiguous
    cr, lf = chr(13), chr(10)
    content = f"first{cr}{lf}second{lf}third"
    tool.apply("mixed.txt", content)

    assert (tmp_path / "mixed.txt").read_bytes() == content.encode("utf-8")


def test_non_ascii_content_round_trips_and_count_is_characters_not_bytes(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    # multi-byte UTF-8 content, where the character count differs from the on-disk byte count
    content = "café — 日本語"
    result = tool.apply("unicode.txt", content)

    target = tmp_path / "unicode.txt"
    # the content round-trips as UTF-8 ...
    assert target.read_text(encoding="utf-8") == content
    assert target.read_bytes() == content.encode("utf-8")
    # ... and the report counts characters, which is strictly fewer than the byte length here
    assert result == f"Created unicode.txt ({len(content)} characters)."
    assert len(content) < target.stat().st_size


def test_bare_filename_skips_makedirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # bare filename + empty root: os.path.dirname is "" so the parent guard skips makedirs
    monkeypatch.chdir(tmp_path)
    tool = _make_tool(project_root="")

    # the bare path resolves under the current working directory (the chdir'd temp dir)
    result = tool.apply("bare.txt", "hi")

    # written verbatim and reported created -- the no-parent-directory branch is exercised
    assert (tmp_path / "bare.txt").read_text(encoding="utf-8") == "hi"
    assert result == "Created bare.txt (2 characters)."


# --- the reflex lifecycle tools: list_dir / find_file / stat / delete_file / rename_file ---
# the service logic is unit-tested in test_file_lifecycle.py; these pin the tool wiring -- that each
# tool resolves the project root, threads the project ignore policy, and renders a typed result.


def test_create_without_overwrite_refuses_existing_and_leaves_original(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    tool.apply("data.txt", "original")
    result = tool.apply("data.txt", "replacement")

    # refused, and the original bytes are intact (no silent clobber)
    assert "already exists" in result
    assert (tmp_path / "data.txt").read_text(encoding="utf-8") == "original"


def test_list_dir_tool_lists_typed_entries(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "sub").mkdir()

    out = _make(ListDirTool, tmp_path).apply(".")
    assert "a.txt" in out
    assert "sub/" in out


def test_find_file_tool_matches_paths_and_never_names_search_for_pattern(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("x")
    (tmp_path / "note.txt").write_text("y")

    out = _make(FindFileTool, tmp_path).apply("*.py")
    assert "mod.py" in out
    assert "note.txt" not in out
    assert "search_for_pattern" not in out


def test_stat_tool_reports_version_and_metadata(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_bytes(b"hello\n")

    out = _make(StatTool, tmp_path).apply("f.txt")
    assert "file" in out
    assert "version " in out
    assert "trailing newline" in out


def test_stat_version_pairs_with_delete_compare_and_swap(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("payload")

    # the version stat prints is exactly the token delete accepts as expect_version
    stat_out = _make(StatTool, tmp_path).apply("f.txt")
    version = stat_out.split("version ")[1].split(",")[0].strip()

    result = _make(DeleteFileTool, tmp_path).apply("f.txt", expect_version=version)
    assert "Deleted" in result
    assert not (tmp_path / "f.txt").exists()


def test_delete_file_tool_is_idempotent(tmp_path: Path) -> None:
    result = _make(DeleteFileTool, tmp_path).apply("ghost.txt")
    assert "nothing to delete" in result


def test_rename_file_tool_moves_and_creates_parents(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")

    result = _make(RenameFileTool, tmp_path).apply("a.txt", "sub/b.txt")
    assert "Renamed" in result
    assert (tmp_path / "sub" / "b.txt").read_text(encoding="utf-8") == "x"
    assert not (tmp_path / "a.txt").exists()


def test_lifecycle_tools_register_with_expected_names_killing_the_deleted_tool_orphan() -> None:
    # the JetBrains overview docstring points agents at `list_dir` and `find_file`; assert those names
    # now resolve to real, registered Tool classes so the deleted-tool signpost no longer dangles.
    from serena.tools.jetbrains_tools import JetBrainsGetSymbolsOverviewTool
    from serena.tools.tools_base import ToolRegistry

    reflex_names = {
        ListDirTool.get_name_from_cls(),
        FindFileTool.get_name_from_cls(),
        StatTool.get_name_from_cls(),
        DeleteFileTool.get_name_from_cls(),
        RenameFileTool.get_name_from_cls(),
    }
    assert reflex_names == {"list_dir", "find_file", "stat", "delete_file", "rename_file"}

    # constructing the registry scans every Tool subclass and raises on any duplicate name, so this
    # also proves the five reflex tools register without colliding with an existing tool
    ToolRegistry()

    doc = JetBrainsGetSymbolsOverviewTool.apply.__doc__ or ""
    for referenced in ("list_dir", "find_file"):
        assert referenced in doc
        assert referenced in reflex_names


@pytest.fixture
def read_file_tool(tmp_path: Path) -> ReadFileTool:
    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.serena_config.default_max_tool_answer_chars = 10000
    return ReadFileTool(agent)


def _deleted_by_delete_lines(content: str, line: int) -> str:
    """
    :return: the text that `delete_lines(line, line)` removes from the given content, which is
        computed via the same primitive the tool uses (`code_editor.delete_lines` deletes the text
        between (line, 0) and (line + 1, 0)).
    """
    _, deleted_text = TextUtils.delete_text_between_positions(content, start_line=line, start_col=0, end_line=line + 1, end_col=0)
    return deleted_text


class TestReadFileToolLineNumbering:
    """
    `delete_lines`/`replace_lines` require the same range of lines to have been read via `read_file`
    beforehand, so the line indices reported by `read_file` must be the ones the editing tools apply to.
    """

    @pytest.mark.parametrize(
        "content",
        [
            "line0\nline1\nline2\n",
            "line0\nline1\nline2",
            "line0\r\nline1\r\nline2\r\n",
            "line0\rline1\rline2\r",
            "line0\n\nline2\n",
            # separators which Python's `splitlines` breaks on but which are not line breaks according to the LSP
            "line0\n\x0cline1\nline2\n",
            "line0\n\x0bline1\nline2\n",
            "line0\n\x1cline1\nline2\n",
            "line0\n\x85line1\nline2\n",
            "line0\n\u2028line1\nline2\n",
        ],
    )
    def test_read_file_lines_match_lines_targeted_by_delete_lines(self, read_file_tool: ReadFileTool, tmp_path: Path, content: str) -> None:
        (tmp_path / "file.txt").write_text(content, newline="", encoding=DEFAULT_SOURCE_FILE_ENCODING)

        for line in range(len(TextUtils.split_lines(content.rstrip("\n")))):
            read_line = read_file_tool.apply("file.txt", start_line=line, end_line=line)
            assert read_line == _deleted_by_delete_lines(content, line).rstrip("\r\n")

    def test_form_feed_does_not_shift_reported_line_indices(self, read_file_tool: ReadFileTool, tmp_path: Path) -> None:
        # a form feed is a page-break convention within a line, not a line break
        (tmp_path / "file.txt").write_text("line0\n\x0cline1\nline2\n", newline="", encoding=DEFAULT_SOURCE_FILE_ENCODING)

        assert read_file_tool.apply("file.txt") == "line0\n\x0cline1\nline2\n"
        assert read_file_tool.apply("file.txt", start_line=1, end_line=1) == "\x0cline1"

    @pytest.mark.parametrize(
        "content",
        ["", "line0", "line0\n", "line0\nline1", "line0\nline1\n", "line0\n\n", "\n", "line0\r\nline1\r\n", "line0\rline1\r"],
    )
    def test_read_full_file_recovers_rejoined_lines(self, read_file_tool: ReadFileTool, tmp_path: Path, content: str) -> None:
        (tmp_path / "file.txt").write_text(content, newline="", encoding=DEFAULT_SOURCE_FILE_ENCODING)

        assert read_file_tool.apply("file.txt") == "\n".join(TextUtils.split_lines(content))
