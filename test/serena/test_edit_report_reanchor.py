"""
Tests for pg://serena/edit/report (task://edit_atomicity.2): once an edit has landed, a failure to re-anchor the tool's
cursor afterwards -- of any exception type, including the language server terminating -- is answered with a reply that the
edit was applied and a note that the cursor could not re-anchor, and raises nothing. A raise would reach the
language-server-termination retry of Tool._issue_call, which runs the whole tool again and applies the edit a second time
(bug://serena/write/edit/atomicity/reanchor).
"""

import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from serena.code_editor import LanguageServerCodeEditor
from serena.cursor import CursorManager
from serena.symbol import LanguageServerSymbolLocation, LanguageServerSymbolRetriever
from serena.tools.cursor_tools import (
    CursorInsertAfterTool,
    CursorInsertAtEndTool,
    CursorInsertAtStartTool,
    CursorInsertBeforeTool,
    CursorRemoveMemberTool,
    CursorRenameTool,
    CursorReplaceBodyTool,
)
from serena.tools.tools_base import SUCCESS_RESULT
from serena.util.file_lifecycle import WritePrecondition
from solidlsp.ls_config import Language
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_process import LanguageServerTerminatedException
from test.conftest import get_repo_path, project_with_ls_context
from test.serena.test_cursor_container_edit import _bind_tool, _make_manager_via_subclass, _ToolHarness

REANCHOR_NOTE = "could not re-anchor"
"""the phrase every tool's reply carries when its cursor could not re-anchor after a landed edit"""

TERMINATED_MESSAGE = "language server terminated during re-anchor"
"""the message of the terminated-language-server failure the re-anchor raises"""

AMBIGUOUS_MESSAGE = "Found multiple 2 symbols matching 'existing'"
"""the message of the ambiguous-match failure the re-anchor raises"""


def _terminated_failure() -> SolidLSPException:
    """
    :return: the exception a language-server request raises when the server has terminated, which the tool layer's retry
        answers by restarting the server and running the whole tool again.
    """
    return SolidLSPException(TERMINATED_MESSAGE, cause=LanguageServerTerminatedException(TERMINATED_MESSAGE, Language.PYTHON))


def _ambiguous_failure() -> ValueError:
    """:return: the exception a re-anchor raises when its name path matches two symbols."""
    return ValueError(AMBIGUOUS_MESSAGE)


FAILURES: list[Callable[[], Exception]] = [_terminated_failure, _ambiguous_failure]
"""the re-anchor failures each structural tool is driven through"""


def _failing_reanchor(manager: CursorManager, failure: Callable[[], Exception]) -> None:
    """
    Make ``manager``'s re-anchor raise the exception ``failure`` builds, as it does when it runs after the edit has landed.

    :param manager: the cursor manager whose re-anchor fails.
    :param failure: the factory of the exception to raise.
    """

    def reanchor_cursor(*args: Any, **kwargs: Any) -> Any:
        raise failure()

    manager.reanchor_cursor = reanchor_cursor  # type: ignore[method-assign]


def _failing_view(manager: CursorManager, failure: Callable[[], Exception]) -> None:
    """
    Make the rendering of ``manager``'s cursor views raise the exception ``failure`` builds, after a re-anchor that succeeds.

    :param manager: the cursor manager whose view rendering fails.
    :param failure: the factory of the exception to raise.
    """

    def format_cursor_view(*args: Any, **kwargs: Any) -> Any:
        raise failure()

    manager.format_cursor_view = format_cursor_view  # type: ignore[method-assign]


class _AgentStandIn:
    """The agent a cursor edit tool reaches: its cursor manager, and the active project behind the tool's ``project``."""

    def __init__(self, manager: CursorManager, project: Any) -> None:
        self._manager = manager
        self._project = project

    def get_cursor_manager(self) -> CursorManager:
        return self._manager

    def get_active_project_or_raise(self) -> Any:
        return self._project


# ----- structural paths --------------------------------------------------------------------------------------------------

JSON_SOURCE = '{\n    "members": {\n        "existing": 1\n    }\n}\n'
"""the JSON document the structural edits are applied to"""


@dataclass(frozen=True)
class StructuralEditCase:
    """One structural edit tool, the arguments it is applied with, and the text the landed edit leaves in the file."""

    tool_cls: type
    cursor_path: str
    apply_kwargs: dict[str, str]
    landed_text: str
    landed_text_count: int

    def __str__(self) -> str:
        return self.tool_cls.__name__


STRUCTURAL_CASES = [
    StructuralEditCase(CursorReplaceBodyTool, "members/existing", {"body": "42"}, '"existing": 42', 1),
    StructuralEditCase(CursorInsertBeforeTool, "members/existing", {"body": '"new_field": 2'}, '"new_field": 2', 1),
    StructuralEditCase(CursorInsertAfterTool, "members/existing", {"body": '"new_field": 2'}, '"new_field": 2', 1),
    StructuralEditCase(CursorInsertAtStartTool, "members", {"body": '"new_field": 2'}, '"new_field": 2', 1),
    StructuralEditCase(CursorInsertAtEndTool, "members", {"body": '"new_field": 2'}, '"new_field": 2', 1),
    StructuralEditCase(CursorRemoveMemberTool, "members/existing", {}, '"existing"', 0),
]
"""every structural edit tool that re-anchors its cursor after its edit lands"""


class TestStructuralEditReanchorFailure:
    """A structural edit tool whose re-anchor fails after its edit has landed."""

    @pytest.mark.parametrize("failure", FAILURES, ids=lambda f: f.__name__)
    @pytest.mark.parametrize("case", STRUCTURAL_CASES, ids=str)
    def test_landed_edit_is_reported_applied(self, case: StructuralEditCase, failure: Callable[[], Exception], work_dir: Path) -> None:
        # a JSON document and a structural cursor on it, whose re-anchor fails
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(work_dir)
        manager.start_cursor(name_path=case.cursor_path, relative_path="doc.json", cursor_id="c1")
        _failing_reanchor(manager, failure)
        tool = _bind_tool(case.tool_cls, _ToolHarness(manager, manager._project))  # type: ignore[attr-defined]

        reply = tool.apply(cursor_id="c1", expect_version="*", **case.apply_kwargs)

        assert reply.startswith(SUCCESS_RESULT)
        assert REANCHOR_NOTE in reply
        assert str(failure()) in reply
        assert edited_path.read_text(encoding="utf-8").count(case.landed_text) == case.landed_text_count

    @pytest.mark.parametrize("case", STRUCTURAL_CASES, ids=str)
    def test_landed_edit_whose_view_fails_is_reported_applied(self, case: StructuralEditCase, work_dir: Path) -> None:
        # a JSON document and a structural cursor on it, whose view fails to render after the edit has landed
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(work_dir)
        manager.start_cursor(name_path=case.cursor_path, relative_path="doc.json", cursor_id="c1")
        _failing_view(manager, _terminated_failure)
        tool = _bind_tool(case.tool_cls, _ToolHarness(manager, manager._project))  # type: ignore[attr-defined]

        reply = tool.apply(cursor_id="c1", expect_version="*", **case.apply_kwargs)

        assert reply.startswith(SUCCESS_RESULT)
        assert REANCHOR_NOTE in reply
        assert str(_terminated_failure()) in reply
        assert edited_path.read_text(encoding="utf-8").count(case.landed_text) == case.landed_text_count


# ----- symbol paths ------------------------------------------------------------------------------------------------------

PYTHON_EDITED_FILE = os.path.join("test_repo", "variables.py")
"""the file of the Python test repository the symbol edits are applied to"""

INSERTED_VARIABLE = 'new_module_var = "Inserted beside typed_module_var"'
"""the statement the insertion tools insert beside ``typed_module_var``"""

REPLACED_BODY = """def modify_instance_var(self):
        # this body was replaced exactly once
        self.instance_var = "Replaced!"
"""
"""the body the replace-body tool gives ``VariableContainer/modify_instance_var``"""


@dataclass
class _SymbolEditFixture:
    """A copy of the Python test repository with a running language server, a cursor manager over it, and a tool binder."""

    project: Any
    manager: CursorManager
    edited_path: Path

    def bind(self, tool_cls: type) -> Any:
        """
        :param tool_cls: the cursor edit tool class to bind.
        :return: an instance of ``tool_cls`` bound to this fixture's cursor manager and project, whose code editors edit
            through this fixture's language server.
        """
        tool = tool_cls.__new__(tool_cls)  # type: ignore[call-overload]
        tool.__dict__["agent"] = _AgentStandIn(self.manager, self.project)
        tool.__dict__["create_code_editor"] = self._create_code_editor
        tool.__dict__["create_ls_code_editor"] = self._create_code_editor
        return tool

    def _create_code_editor(self, write_precondition: WritePrecondition | None = None) -> LanguageServerCodeEditor:
        return LanguageServerCodeEditor(LanguageServerSymbolRetriever(self.project), write_precondition=write_precondition)


@contextmanager
def _symbol_edit_fixture(work_dir: Path) -> Iterator[_SymbolEditFixture]:
    """
    :param work_dir: the directory the Python test repository is copied into.
    :return: a context yielding the fixture over the copy, whose language server stops when the context exits.
    """
    repo_path = work_dir / get_repo_path(Language.PYTHON).name
    shutil.copytree(get_repo_path(Language.PYTHON), repo_path)
    with project_with_ls_context(Language.PYTHON, str(repo_path)) as project:
        yield _SymbolEditFixture(project=project, manager=CursorManager(project=project), edited_path=repo_path / PYTHON_EDITED_FILE)


@pytest.mark.python
class TestSymbolEditReanchorFailure:
    """A language-server symbol edit tool whose re-anchor fails after its edit has landed."""

    @pytest.mark.parametrize("failure", FAILURES, ids=lambda f: f.__name__)
    @pytest.mark.parametrize("tool_cls", [CursorInsertBeforeTool, CursorInsertAfterTool], ids=lambda c: c.__name__)
    def test_landed_insertion_is_reported_applied(self, tool_cls: type, failure: Callable[[], Exception], work_dir: Path) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            fixture.manager.start_cursor(name_path="typed_module_var", relative_path=PYTHON_EDITED_FILE, cursor_id="c1")
            _failing_reanchor(fixture.manager, failure)

            reply = fixture.bind(tool_cls).apply(cursor_id="c1", body=INSERTED_VARIABLE, expect_version="*")

            contents = fixture.edited_path.read_text(encoding="utf-8")
        assert reply.startswith(SUCCESS_RESULT)
        assert REANCHOR_NOTE in reply
        assert str(failure()) in reply
        assert contents.count(INSERTED_VARIABLE) == 1

    def test_landed_body_replacement_is_reported_applied(self, work_dir: Path) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            fixture.manager.start_cursor(
                name_path="VariableContainer/modify_instance_var", relative_path=PYTHON_EDITED_FILE, cursor_id="c1"
            )
            _failing_reanchor(fixture.manager, _terminated_failure)

            reply = fixture.bind(CursorReplaceBodyTool).apply(cursor_id="c1", body=REPLACED_BODY, expect_version="*")

            contents = fixture.edited_path.read_text(encoding="utf-8")
        assert reply.startswith(SUCCESS_RESULT)
        assert REANCHOR_NOTE in reply
        assert TERMINATED_MESSAGE in reply
        assert contents.count("# this body was replaced exactly once") == 1

    def test_landed_rename_is_reported_applied(self, work_dir: Path) -> None:
        with _symbol_edit_fixture(work_dir) as fixture:
            occurrences_before = fixture.edited_path.read_text(encoding="utf-8").count("typed_module_var")
            fixture.manager.start_cursor(name_path="typed_module_var", relative_path=PYTHON_EDITED_FILE, cursor_id="c1")
            _failing_reanchor(fixture.manager, _terminated_failure)

            reply = fixture.bind(CursorRenameTool).apply(cursor_id="c1", new_name="renamed_typed_module_var")

            contents = fixture.edited_path.read_text(encoding="utf-8")
        assert reply.startswith("Successfully renamed 'typed_module_var' to 'renamed_typed_module_var'")
        assert "edit applied" in reply
        assert REANCHOR_NOTE in reply
        assert TERMINATED_MESSAGE in reply
        assert contents.count("renamed_typed_module_var") == occurrences_before
        assert contents.count("renamed_renamed_typed_module_var") == 0


class TestStructuralOnlyToolGuards:
    """The route of the structural-only edit tools that refuses a language-server cursor."""

    @pytest.mark.parametrize(
        "tool_cls", [CursorInsertAtStartTool, CursorInsertAtEndTool, CursorRemoveMemberTool], ids=lambda value: value.__name__
    )
    def test_language_server_cursor_is_refused(self, tool_cls: type, work_dir: Path) -> None:
        """Regression guard: a structural-only tool given a language-server cursor raises TypeError and leaves the file as it was."""
        with _symbol_edit_fixture(work_dir) as fixture:
            # a language-server cursor, which no structural-only tool edits through
            bytes_before = fixture.edited_path.read_bytes()
            fixture.manager.start_cursor(name_path="typed_module_var", relative_path=PYTHON_EDITED_FILE, cursor_id="c1")
            body_kwargs = {} if tool_cls is CursorRemoveMemberTool else {"body": INSERTED_VARIABLE}

            with pytest.raises(TypeError):
                fixture.bind(tool_cls).apply(cursor_id="c1", expect_version="*", **body_kwargs)
            bytes_after = fixture.edited_path.read_bytes()

        assert bytes_after == bytes_before

    def test_top_level_member_removal_is_refused(self, work_dir: Path) -> None:
        """Regression guard: a top-level member has no parent container to be removed from, so its removal is refused and the file is left as it was."""
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        bytes_before = edited_path.read_bytes()
        manager = _make_manager_via_subclass(work_dir)
        manager.start_cursor(name_path="members", relative_path="doc.json", cursor_id="c1")
        tool = _bind_tool(CursorRemoveMemberTool, _ToolHarness(manager, manager._project))  # type: ignore[attr-defined]

        with pytest.raises(ValueError, match="no parent container"):
            tool.apply(cursor_id="c1", expect_version="*")

        assert edited_path.read_bytes() == bytes_before


SHRUNK_CLASS_MARKER = "# this class body was replaced exactly once"
"""the comment the shrinking body replacement leaves in the edited file"""

SHRUNK_CLASS = f"""class VariableContainer:
    {SHRUNK_CLASS_MARKER}
    pass
"""
"""a body for ``VariableContainer`` that is a small fraction of the class it replaces"""

OUTSIDE_PROJECT_CASES: list[tuple[type, dict[str, str]]] = [
    (CursorReplaceBodyTool, {"body": REPLACED_BODY, "expect_version": "*"}),
    (CursorInsertBeforeTool, {"body": INSERTED_VARIABLE, "expect_version": "*"}),
    (CursorInsertAfterTool, {"body": INSERTED_VARIABLE, "expect_version": "*"}),
    (CursorRenameTool, {"new_name": "renamed_typed_module_var"}),
]
"""every language-server symbol edit tool that refuses a cursor whose symbol has no project-relative path, with its arguments"""


@pytest.mark.python
class TestSymbolEditToolGuards:
    """The routes of the language-server symbol edit tools that refuse an edit, or report one that landed in a surprising state."""

    @pytest.mark.parametrize(
        ("tool_cls", "apply_kwargs"), OUTSIDE_PROJECT_CASES, ids=lambda value: value.__name__ if isinstance(value, type) else ""
    )
    def test_cursor_on_a_symbol_outside_the_project_is_refused(self, tool_cls: type, apply_kwargs: dict[str, str], work_dir: Path) -> None:
        """Regression guard: a cursor on a symbol defined outside the project has no project-relative path to edit, so the edit is refused and the file is left as it was."""
        with _symbol_edit_fixture(work_dir) as fixture:
            bytes_before = fixture.edited_path.read_bytes()
            fixture.manager.start_cursor(name_path="typed_module_var", relative_path=PYTHON_EDITED_FILE, cursor_id="c1")
            state = fixture.manager.get_lsp_cursor("c1")
            # the cursor's symbol as one defined outside the project, whose location carries no project-relative path
            state.current_location = LanguageServerSymbolLocation(
                relative_path=None, line=state.current_location.line, column=state.current_location.column
            )

            with pytest.raises(ValueError, match="has no relative path"):
                fixture.bind(tool_cls).apply(cursor_id="c1", **apply_kwargs)
            bytes_after = fixture.edited_path.read_bytes()

        assert bytes_after == bytes_before

    def test_replacement_removing_far_more_than_its_symbol_spans_raises_as_applied(
        self, work_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Regression guard: a body replacement whose diff removes far more lines than the cursor's symbol spans raises after the
        edit has landed, with a message that the edit has already been applied, and the edit is applied once. The raise is a
        ValueError, not the SolidLSPException reporting a terminated language server that the retry of Tool._issue_call
        answers by running the tool a second time.
        """
        with _symbol_edit_fixture(work_dir) as fixture:
            fixture.manager.start_cursor(name_path="VariableContainer", relative_path=PYTHON_EDITED_FILE, cursor_id="c1")
            symbol = fixture.manager.get_lsp_cursor("c1").current_symbol
            # the cursor's record of the symbol's extent reduced to its first line, as when the extent the edit replaces has
            # grown past the one the cursor holds
            monkeypatch.setattr(symbol, "get_body_end_position_or_raise", symbol.get_body_start_position_or_raise)

            with pytest.raises(ValueError, match="has already been applied"):
                fixture.bind(CursorReplaceBodyTool).apply(cursor_id="c1", body=SHRUNK_CLASS, expect_version="*")
            contents = fixture.edited_path.read_text(encoding="utf-8")

        assert contents.count(SHRUNK_CLASS_MARKER) == 1
