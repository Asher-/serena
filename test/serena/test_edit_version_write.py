"""
Tests for pg://serena/edit/version (task://edit_atomicity.3): an edit given an expect_version other than "*" compares it with
the edited file's on-disk version immediately before renaming its new contents into place, so a change another writer makes
after the tool's version check has passed is neither overwritten nor edited on top of, and the tool replies with the file's
stale state (bug://serena/write/edit/atomicity/reanchor).
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import serena.tools.cursor_tools as cursor_tools
from serena.cursor import CursorManager
from serena.tools.cursor_tools import (
    CursorInsertAfterTool,
    CursorInsertAtEndTool,
    CursorInsertBeforeTool,
    CursorReplaceBodyTool,
    CursorReplaceRangeTool,
)
from serena.util.file_lifecycle import content_version
from test.serena.test_cursor_container_edit import _make_manager_via_subclass
from test.serena.test_edit_report_reanchor import (
    INSERTED_VARIABLE,
    PYTHON_EDITED_FILE,
    REPLACED_BODY,
    STRUCTURAL_CASES,
    StructuralEditCase,
    _AgentStandIn,
    _symbol_edit_fixture,
)

STALE_MARKER = "changed under you"
"""the phrase the rendered stale state of a file carries"""

JSON_SOURCE = '{\n    "members": {\n        "existing": 1\n    }\n}\n'
"""the JSON document the structural edit is applied to"""


@dataclass
class _ConcurrentWriter:
    """
    Another writer that changes a file right after a cursor edit tool's version check has passed and before the tool
    writes, recording the file's bytes as it leaves them.
    """

    path: Path
    change: Callable[[str], str]
    bytes_after_change: bytes | None = field(default=None, init=False)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Follow every passing version check of the cursor edit tools with this writer's change.

        :param monkeypatch: the fixture through which the version check is wrapped.
        """
        original = cursor_tools._refuse_if_stale

        def refuse_if_stale(tool: Any, relative_path: str, expect_version: str) -> str | None:
            refusal = original(tool, relative_path, expect_version)
            if refusal is None:
                self.path.write_text(self.change(self.path.read_text(encoding="utf-8")), encoding="utf-8")
                self.bytes_after_change = self.path.read_bytes()
            return refusal

        monkeypatch.setattr(cursor_tools, "_refuse_if_stale", refuse_if_stale)


def _append_python_variable(contents: str) -> str:
    """:return: ``contents`` with a variable assignment appended, which moves no existing symbol."""
    return contents + "concurrent_var = 1\n"


def _change_json_value(contents: str) -> str:
    """:return: ``contents`` with the value of ``existing`` changed, which keeps the document valid JSON."""
    return contents.replace('"existing": 1', '"existing": 7')


def _bind_structural(tool_cls: type, manager: CursorManager) -> Any:
    """
    :param tool_cls: the structural cursor edit tool class to bind.
    :param manager: the cursor manager the tool edits through, whose project roots the tool's version check.
    :return: an instance of ``tool_cls`` bound to ``manager`` and its project.
    """
    tool = tool_cls.__new__(tool_cls)  # type: ignore[call-overload]
    tool.__dict__["agent"] = _AgentStandIn(manager, manager._project)  # type: ignore[attr-defined]
    return tool


class TestStructuralEditVersionAtWrite:
    """A structural edit whose file another writer changes after the tool's version check has passed."""

    def test_change_after_version_check_is_kept_and_reported_stale(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # a JSON document, the version the caller read it at, and a structural cursor on its container
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        expect_version = content_version(edited_path.read_bytes())
        listing_before = sorted(os.listdir(work_dir))
        manager = _make_manager_via_subclass(work_dir)
        manager.start_cursor(name_path="members", relative_path="doc.json", cursor_id="c1")
        writer = _ConcurrentWriter(edited_path, _change_json_value)
        writer.install(monkeypatch)
        tool = _bind_structural(CursorInsertAtEndTool, manager)

        reply = tool.apply(cursor_id="c1", body='"new_field": 2', expect_version=expect_version)

        assert writer.bytes_after_change is not None
        assert edited_path.read_bytes() == writer.bytes_after_change
        assert sorted(os.listdir(work_dir)) == listing_before
        assert STALE_MARKER in reply

    def test_unconditional_edit_writes_over_the_change(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: expect_version "*" checks no version, so the edit lands on the changed file."""
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(work_dir)
        manager.start_cursor(name_path="members", relative_path="doc.json", cursor_id="c1")
        _ConcurrentWriter(edited_path, _change_json_value).install(monkeypatch)
        tool = _bind_structural(CursorInsertAtEndTool, manager)

        reply = tool.apply(cursor_id="c1", body='"new_field": 2', expect_version="*")

        contents = edited_path.read_text(encoding="utf-8")
        assert STALE_MARKER not in reply
        assert '"existing": 7' in contents
        assert '"new_field": 2' in contents

    @pytest.mark.parametrize("case", STRUCTURAL_CASES, ids=str)
    def test_every_structural_tool_keeps_a_change_after_its_version_check(
        self, case: StructuralEditCase, work_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a JSON document, the version the caller read it at, and a structural cursor for the tool
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        expect_version = content_version(edited_path.read_bytes())
        listing_before = sorted(os.listdir(work_dir))
        manager = _make_manager_via_subclass(work_dir)
        manager.start_cursor(name_path=case.cursor_path, relative_path="doc.json", cursor_id="c1")
        writer = _ConcurrentWriter(edited_path, _change_json_value)
        writer.install(monkeypatch)

        reply = _bind_structural(case.tool_cls, manager).apply(cursor_id="c1", expect_version=expect_version, **case.apply_kwargs)

        assert writer.bytes_after_change is not None
        assert edited_path.read_bytes() == writer.bytes_after_change
        assert sorted(os.listdir(work_dir)) == listing_before
        assert STALE_MARKER in reply

    @pytest.mark.parametrize("case", STRUCTURAL_CASES, ids=str)
    def test_every_structural_tool_refuses_a_stale_version_before_its_edit(self, case: StructuralEditCase, work_dir: Path) -> None:
        """Regression guard: a version the file no longer has is refused before the edit is computed, leaving the file as it was."""
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        bytes_before = edited_path.read_bytes()
        manager = _make_manager_via_subclass(work_dir)
        manager.start_cursor(name_path=case.cursor_path, relative_path="doc.json", cursor_id="c1")

        reply = _bind_structural(case.tool_cls, manager).apply(
            cursor_id="c1", expect_version=content_version(b"other bytes"), **case.apply_kwargs
        )

        assert edited_path.read_bytes() == bytes_before
        assert STALE_MARKER in reply


@pytest.mark.python
class TestSymbolAndRangeEditVersionAtWrite:
    """A language-server symbol or range edit whose file another writer changes after the tool's version check has passed."""

    @pytest.mark.parametrize(
        ("tool_cls", "apply_kwargs"),
        [
            (CursorReplaceBodyTool, {"cursor_id": "c1", "body": REPLACED_BODY}),
            (CursorInsertBeforeTool, {"cursor_id": "c1", "body": INSERTED_VARIABLE}),
            (CursorInsertAfterTool, {"cursor_id": "c1", "body": INSERTED_VARIABLE}),
            (
                CursorReplaceRangeTool,
                {"relative_path": PYTHON_EDITED_FILE, "start_line": 1, "end_line": 1, "body": "# the first line, replaced\n"},
            ),
        ],
        ids=lambda value: value.__name__ if isinstance(value, type) else "",
    )
    def test_change_after_version_check_is_kept_and_reported_stale(
        self, tool_cls: type, apply_kwargs: dict[str, Any], work_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            # the version the caller read the file at, and a cursor on the edited symbol
            expect_version = content_version(fixture.edited_path.read_bytes())
            listing_before = sorted(os.listdir(fixture.edited_path.parent))
            name_path = "VariableContainer/modify_instance_var" if tool_cls is CursorReplaceBodyTool else "typed_module_var"
            fixture.manager.start_cursor(name_path=name_path, relative_path=PYTHON_EDITED_FILE, cursor_id="c1")
            writer = _ConcurrentWriter(fixture.edited_path, _append_python_variable)
            writer.install(monkeypatch)

            reply = fixture.bind(tool_cls).apply(expect_version=expect_version, **apply_kwargs)

            bytes_after = fixture.edited_path.read_bytes()
            listing_after = sorted(os.listdir(fixture.edited_path.parent))
        assert writer.bytes_after_change is not None
        assert bytes_after == writer.bytes_after_change
        assert listing_after == listing_before
        assert STALE_MARKER in reply

    @pytest.mark.parametrize(
        ("tool_cls", "apply_kwargs"),
        [
            (CursorReplaceBodyTool, {"cursor_id": "c1", "body": REPLACED_BODY}),
            (CursorInsertBeforeTool, {"cursor_id": "c1", "body": INSERTED_VARIABLE}),
            (CursorInsertAfterTool, {"cursor_id": "c1", "body": INSERTED_VARIABLE}),
        ],
        ids=lambda value: value.__name__ if isinstance(value, type) else "",
    )
    def test_stale_version_is_refused_before_the_edit(self, tool_cls: type, apply_kwargs: dict[str, Any], work_dir: Path) -> None:
        """Regression guard: a version the file no longer has is refused before the edit is computed, leaving the file as it was."""
        with _symbol_edit_fixture(work_dir) as fixture:
            # a cursor on the edited symbol, and a version the file does not have
            bytes_before = fixture.edited_path.read_bytes()
            name_path = "VariableContainer/modify_instance_var" if tool_cls is CursorReplaceBodyTool else "typed_module_var"
            fixture.manager.start_cursor(name_path=name_path, relative_path=PYTHON_EDITED_FILE, cursor_id="c1")

            reply = fixture.bind(tool_cls).apply(expect_version=content_version(b"other bytes"), **apply_kwargs)
            bytes_after = fixture.edited_path.read_bytes()

        assert bytes_after == bytes_before
        assert STALE_MARKER in reply
