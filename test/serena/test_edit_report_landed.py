"""
Tests for pg://serena/edit/report beyond the re-anchor: once an edit's new contents are in place on disk, a failure of a
step after the write -- here the release of the file's language-server buffer in a terminated language server, which
raises BrokenPipeError as writing the didClose notification to the dead server's stdin does -- is answered with a reply
that the edit was applied, and the edit is applied exactly once. Before the change such a failure propagated out of the
tool as if the edit had not happened. And for pg://serena/edit/write: a failed save whose discard of the unsaved contents
fails as well still raises the save's own failure. The JetBrains refresh failure is tested in test_edit_editor_backend.py.

The module builds on the language-server fixture of test_edit_report_reanchor.py, which serena main's src cannot import, so
its tests of new behaviour are observed failing against the branch's src before the change.
"""

from pathlib import Path

import pytest

from serena.tools.cursor_tools import (
    CursorInsertAfterTool,
    CursorInsertBeforeTool,
    CursorReplaceBetweenTool,
    CursorReplaceBodyTool,
    CursorReplaceRangeTool,
)
from serena.tools.tools_base import SUCCESS_RESULT
from solidlsp.ls import LSPFileBuffer
from test.serena.test_edit_editor_backend import EDITED_FILE, REPLACED_LINE
from test.serena.test_edit_report_reanchor import (
    INSERTED_VARIABLE,
    PYTHON_EDITED_FILE,
    REPLACED_BODY,
    _symbol_edit_fixture,
)

APPLIED_NOTE = "has been applied"
"""the phrase a reply carries when a step after an edit's write failed"""

CAUSE_MESSAGE = "the IDE could not be reached"
"""the message of the failure after a write that the error is built from"""

RELEASE_FAILURE_MESSAGE = "the language server's stdin is closed"
"""the message of the failure closing the edited file's language-server buffer raises after the save"""

DISCARD_FAILURE_MESSAGE = "the edited file vanished"
"""the message of the failure reloading the edited file's language-server buffer raises after a failed save"""

DISCARD_NOTE = "Discarding the unsaved contents"
"""the phrase of the note a failed save's error carries when discarding the unsaved contents failed as well"""

REPLACED_INTERSTITIAL = "# the lines between, replaced exactly once\n"
"""the text cursor_replace_between puts between ``typed_module_var`` and ``VariableContainer``"""


class _ReleaseFailureAfterSave:
    """
    A terminated language server, as closing the edited file's buffer meets it: the first close of the edited file's buffer
    once the file on disk holds an edit's marker raises BrokenPipeError, as writing the didClose notification to a dead
    server's stdin does.
    """

    def __init__(self, path: Path, marker: str) -> None:
        """
        :param path: the edited file.
        :param marker: text the edited file holds once the edit's save has landed.
        """
        self._path = path
        self._marker = marker
        self.raised = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        Make the first close of the edited file's buffer after the save fail.

        :param monkeypatch: the fixture through which the buffer's close is wrapped.
        """
        original = LSPFileBuffer.close

        def close(buffer: LSPFileBuffer) -> None:
            if not self.raised and Path(buffer.abs_path) == self._path and self._marker in self._path.read_text(encoding="utf-8"):
                self.raised = True
                raise BrokenPipeError(RELEASE_FAILURE_MESSAGE)
            original(buffer)

        monkeypatch.setattr(LSPFileBuffer, "close", close)


@pytest.mark.python
class TestLanguageServerReleaseFailure:
    """An edit through the language-server code editor whose buffer release fails after the save has landed."""

    @pytest.mark.parametrize("tool_cls", [CursorInsertBeforeTool, CursorInsertAfterTool], ids=lambda value: value.__name__)
    def test_landed_insertion_is_reported_applied(self, tool_cls: type, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            fixture.manager.start_cursor(name_path="typed_module_var", relative_path=PYTHON_EDITED_FILE, cursor_id="c1")
            release_failure = _ReleaseFailureAfterSave(fixture.edited_path, INSERTED_VARIABLE)
            release_failure.install(monkeypatch)

            reply = fixture.bind(tool_cls).apply(cursor_id="c1", body=INSERTED_VARIABLE, expect_version="*")

            contents = fixture.edited_path.read_text(encoding="utf-8")
        assert release_failure.raised
        assert reply.startswith(SUCCESS_RESULT)
        assert APPLIED_NOTE in reply
        assert RELEASE_FAILURE_MESSAGE in reply
        assert contents.count(INSERTED_VARIABLE) == 1

    def test_landed_body_replacement_is_reported_applied(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        marker = "# this body was replaced exactly once"
        with _symbol_edit_fixture(work_dir) as fixture:
            fixture.manager.start_cursor(
                name_path="VariableContainer/modify_instance_var", relative_path=PYTHON_EDITED_FILE, cursor_id="c1"
            )
            release_failure = _ReleaseFailureAfterSave(fixture.edited_path, marker)
            release_failure.install(monkeypatch)

            reply = fixture.bind(CursorReplaceBodyTool).apply(cursor_id="c1", body=REPLACED_BODY, expect_version="*")

            contents = fixture.edited_path.read_text(encoding="utf-8")
        assert release_failure.raised
        assert reply.startswith(SUCCESS_RESULT)
        assert "Diff: " in reply
        assert APPLIED_NOTE in reply
        assert RELEASE_FAILURE_MESSAGE in reply
        assert contents.count(marker) == 1

    def test_landed_range_replacement_is_reported_applied(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            release_failure = _ReleaseFailureAfterSave(fixture.edited_path, REPLACED_LINE)
            release_failure.install(monkeypatch)

            reply = fixture.bind(CursorReplaceRangeTool).apply(
                relative_path=PYTHON_EDITED_FILE, start_line=1, end_line=1, body=REPLACED_LINE, expect_version="*"
            )

            contents = fixture.edited_path.read_text(encoding="utf-8")
        assert release_failure.raised
        assert reply.startswith(SUCCESS_RESULT)
        assert APPLIED_NOTE in reply
        assert RELEASE_FAILURE_MESSAGE in reply
        assert contents.count(REPLACED_LINE) == 1

    def test_landed_interstitial_replacement_is_reported_applied(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            release_failure = _ReleaseFailureAfterSave(fixture.edited_path, REPLACED_INTERSTITIAL)
            release_failure.install(monkeypatch)

            reply = fixture.bind(CursorReplaceBetweenTool).apply(
                relative_path=PYTHON_EDITED_FILE,
                before_symbol="typed_module_var",
                after_symbol="VariableContainer",
                body=REPLACED_INTERSTITIAL,
                expect_version="*",
            )

            contents = fixture.edited_path.read_text(encoding="utf-8")
        assert release_failure.raised
        assert reply.startswith(SUCCESS_RESULT)
        assert APPLIED_NOTE in reply
        assert RELEASE_FAILURE_MESSAGE in reply
        assert contents.count(REPLACED_INTERSTITIAL) == 1


@pytest.mark.python
class TestFailedSaveDiscardFailure:
    """A save that fails before replacing the file, whose discard of the unsaved contents fails as well."""

    def test_save_failure_is_raised_with_a_note_of_the_discard_failure(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            # a project whose encoding cannot represent the new body, and a buffer reload that fails
            fixture.project.project_config.encoding = "ascii"
            bytes_before = fixture.edited_path.read_bytes()
            original_reload = LSPFileBuffer.reload_from_disk

            def reload_from_disk(buffer: LSPFileBuffer, force: bool = False) -> None:
                if force:
                    raise FileNotFoundError(DISCARD_FAILURE_MESSAGE)
                original_reload(buffer)

            monkeypatch.setattr(LSPFileBuffer, "reload_from_disk", reload_from_disk)
            code_editor = fixture.bind(CursorReplaceBodyTool).create_code_editor()

            with pytest.raises(UnicodeEncodeError) as raised:
                code_editor.replace_body(
                    "VariableContainer/modify_instance_var", PYTHON_EDITED_FILE, REPLACED_BODY.replace("Replaced!", "Replaced ☃")
                )

            bytes_after = fixture.edited_path.read_bytes()
        assert bytes_after == bytes_before
        notes = getattr(raised.value, "__notes__", [])
        assert any(DISCARD_NOTE in note and DISCARD_FAILURE_MESSAGE in note for note in notes)


class TestLandedEditError:
    """The error an edit raises once its new contents are in place on disk."""

    def test_message_names_the_file_and_the_failure(self) -> None:
        # imported here so that the module's other tests run against the branch's src before the change, which lacks the class
        from serena.code_editor import LandedEditError

        cause = ConnectionError(CAUSE_MESSAGE)

        error = LandedEditError(EDITED_FILE, cause)

        assert error.relative_path == EDITED_FILE
        assert error.cause is cause
        assert EDITED_FILE in str(error)
        assert APPLIED_NOTE in str(error)
        assert CAUSE_MESSAGE in str(error)
