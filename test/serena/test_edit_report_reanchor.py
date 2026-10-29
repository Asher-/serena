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
from serena.symbol import LanguageServerSymbolRetriever
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

    def _create_code_editor(self) -> LanguageServerCodeEditor:
        return LanguageServerCodeEditor(LanguageServerSymbolRetriever(self.project))


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
        assert REANCHOR_NOTE in reply
        assert TERMINATED_MESSAGE in reply
        assert contents.count("renamed_typed_module_var") == occurrences_before
        assert contents.count("renamed_renamed_typed_module_var") == 0
