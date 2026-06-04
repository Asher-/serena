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

import pytest

from serena.tools.file_tools import CreateTextFileTool


@dataclass
class _ProjectStub:
    """minimal stand-in for :class:`Project`.

    exposes only ``project_root`` -- the sole attribute :meth:`Tool.get_project_root` reads off
    the active project.
    """

    project_root: str


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

    # seed a file, then overwrite it through the tool
    tool.apply("data.txt", "original contents")
    result = tool.apply("data.txt", "replaced!")

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
